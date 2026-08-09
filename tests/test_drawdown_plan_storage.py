from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fund_alert_bot.checks import evaluate_drawdown_plan_rule
from fund_alert_bot.db import (
    add_rule,
    delete_rule,
    get_active_drawdown_cycle,
    initialize_database,
    list_drawdown_tier_records,
    list_retryable_drawdown_plan_alert_events,
    list_rules,
    open_connection,
    persist_drawdown_plan_evaluation,
    record_alert_notification_result,
)
from fund_alert_bot.market_data import AssetType
from fund_alert_bot.rules.drawdown_plan import DrawdownTier


def test_confirmed_evaluation_persists_one_cycle_tiers_and_aggregate_event(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    rule_id = _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        result = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 74]),
            expected_date=date(2024, 1, 2),
        )
        cycle = get_active_drawdown_cycle(connection, rule_id)
        tiers = list_drawdown_tier_records(connection, result.cycle_id)
        events = connection.execute("SELECT * FROM alert_events").fetchall()

    assert cycle is not None
    assert cycle["peak_date"] == "2024-01-01"
    assert [row["tier_key"] for row in tiers] == ["0.15", "0.2", "0.25"]
    assert len({row["alert_event_id"] for row in tiers}) == 1
    assert len(events) == 1
    assert result.notification is not None
    assert json.loads(events[0]["payload_json"])["total_amount"] == 30000


def test_restart_preserves_tier_deduplication_and_allows_next_level(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84, 79]),
            expected_date=date(2024, 1, 3),
        )
        tiers = list_drawdown_tier_records(connection, result.cycle_id)
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    assert [row["tier_key"] for row in tiers] == ["0.15", "0.2"]
    assert result.evaluation.total_amount == 10000
    assert event_count == 2


def test_new_peak_closes_old_cycle_and_rearms_tier(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    rule_id = _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        first = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        second = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84, 101, 85.85]),
            expected_date=date(2024, 1, 4),
        )
        cycles = connection.execute(
            "SELECT id, peak_date, end_date FROM drawdown_cycles WHERE rule_id = ?",
            (rule_id,),
        ).fetchall()
        second_tiers = list_drawdown_tier_records(connection, second.cycle_id)

    assert second.cycle_id != first.cycle_id
    assert [(row["peak_date"], row["end_date"]) for row in cycles] == [
        ("2024-01-01", "2024-01-03"),
        ("2024-01-03", None),
    ]
    assert [row["tier_key"] for row in second_tiers] == ["0.15"]


def test_confirmed_evaluation_rejects_empty_plan_name(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        connection.execute("UPDATE rules SET name = ' '")
        connection.commit()

        with pytest.raises(ValueError, match="name must not be empty"):
            evaluate_drawdown_plan_rule(
                connection,
                list_rules(connection)[0],
                _history([100, 84]),
                expected_date=date(2024, 1, 2),
            )

        assert get_active_drawdown_cycle(connection, 1) is None


def test_delete_soft_disables_plan_and_preserves_cycle_state(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    rule_id = _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )

        removal = delete_rule(connection, rule_id)
        rule = list_rules(connection)[0]
        cycle = get_active_drawdown_cycle(connection, rule_id)
        tiers = list_drawdown_tier_records(connection, result.cycle_id)

    assert removal is True
    assert rule["enabled"] == 0
    assert cycle is not None
    assert [row["tier_key"] for row in tiers] == ["0.15"]


def test_failed_or_interrupted_plan_notifications_are_retryable(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        assert result.notification is not None
        pending = list_retryable_drawdown_plan_alert_events(connection)
        record_alert_notification_result(
            connection,
            event_id=result.notification.event_id,
            results=[{"channel": "telegram", "success": False, "detail": "failed"}],
        )
        failed = list_retryable_drawdown_plan_alert_events(connection)
        record_alert_notification_result(
            connection,
            event_id=result.notification.event_id,
            results=[{"channel": "telegram", "success": True, "detail": "sent"}],
        )
        sent = list_retryable_drawdown_plan_alert_events(connection)

    assert [row["notification_status"] for row in pending] == ["pending"]
    assert [row["notification_status"] for row in failed] == ["failed"]
    assert sent == []


def test_cycle_tiers_and_event_roll_back_together_on_constraint_failure(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    rule_id = _add_plan(sqlite_path)
    duplicate_tier = DrawdownTier(0.15, 5000, "0.15")
    alert = {
        "alert_key": "test-aggregate",
        "title": "test",
        "message": "test",
        "payload": {},
    }

    with open_connection(sqlite_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            persist_drawdown_plan_evaluation(
                connection,
                rule_id=rule_id,
                expected_active_cycle_id=None,
                start_new_cycle=True,
                peak_date="2024-01-01",
                peak_price=100,
                evaluation_date="2024-01-02",
                tiers=[duplicate_tier, duplicate_tier],
                alert=alert,
            )
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "drawdown_cycles",
                "drawdown_tier_records",
                "alert_events",
            )
        ]

    assert counts == [0, 0, 0]


def _add_plan(sqlite_path: Path) -> int:
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        return add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="A500",
            asset_type=AssetType.CN_ETF.value,
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "tiers": [
                    {"drawdown": 0.15, "amount": 5000},
                    {"drawdown": 0.20, "amount": 10000},
                    {"drawdown": 0.25, "amount": 15000},
                ],
                "sma_window": 250,
                "sma_slope_window": 20,
            },
        )


def _history(closes: list[float]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(closes)),
            "close": closes,
            "source": ["akshare_eastmoney"] * len(closes),
        }
    )
    frame.attrs.update(
        {
            "symbol": "510300",
            "source": "akshare_eastmoney",
            "price_basis": "qfq",
            "frequency": "daily",
        }
    )
    return frame
