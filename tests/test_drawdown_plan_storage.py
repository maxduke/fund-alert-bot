from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from fund_alert_bot.checks import (
    derive_plan_readiness,
    evaluate_drawdown_plan_rule,
    evaluate_drawdown_plan_rules,
    read_drawdown_plan_statuses,
)
from fund_alert_bot.db import (
    add_rule,
    apply_manual_add_estimate,
    delete_rule,
    get_active_drawdown_cycle,
    get_fund_settings,
    get_position_snapshot,
    initialize_database,
    list_drawdown_tier_records,
    list_manual_add_actions,
    list_pending_position_items,
    list_retryable_drawdown_plan_alert_events,
    list_rules,
    open_connection,
    persist_drawdown_plan_evaluation,
    reconcile_position_snapshot,
    record_alert_notification_result,
    record_manual_addition,
    upsert_fund_fee,
    upsert_position_snapshot,
)
from fund_alert_bot.market_data import AssetType, FundNav, Instrument, PriceBasis
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


def test_batch_confirmed_close_fetches_qfq_once_and_persists_plan(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    provider = FakePlanProvider(_history([100, 80]))

    with open_connection(sqlite_path) as connection:
        result = evaluate_drawdown_plan_rules(
            connection,
            provider,
            expected_date=date(2024, 1, 2),
        )
        cycle_count = connection.execute(
            "SELECT COUNT(*) FROM drawdown_cycles"
        ).fetchone()[0]
        tier_count = connection.execute(
            "SELECT COUNT(*) FROM drawdown_tier_records"
        ).fetchone()[0]

    assert result.checked_rules == 1
    assert len(result.notifications) == 1
    assert result.errors == []
    assert provider.calls[0][0].symbol == "510300"
    assert provider.calls[0][3] is PriceBasis.QFQ
    assert cycle_count == 1
    assert tier_count == 2


def test_batch_confirmed_close_rejects_stale_history_without_state(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        result = evaluate_drawdown_plan_rules(
            connection,
            FakePlanProvider(_history([80])),
            expected_date=date(2024, 1, 2),
        )
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("drawdown_cycles", "drawdown_tier_records", "alert_events")
        ]

    assert len(result.errors) == 1
    assert "does not contain closing data for 2024-01-02" in result.errors[0].message
    assert counts == [0, 0, 0]


def test_read_plan_status_is_pure_and_reports_open_tiers(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    provider = FakePlanProvider(_history([100, 80]))

    with open_connection(sqlite_path) as connection:
        result = read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 2),
        )
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("drawdown_cycles", "drawdown_tier_records", "alert_events")
        ]

    assert len(result.statuses) == 1
    status = result.statuses[0]
    assert status.evaluation.drawdown == pytest.approx(0.20)
    assert status.recorded_tier_keys == frozenset()
    assert status.readiness == "SETUP_REQUIRED"
    assert status.missing_setup == ("fund fee", "position snapshot")
    assert counts == [0, 0, 0]


def test_read_plan_status_does_not_reuse_tiers_after_new_peak(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        result = read_drawdown_plan_statuses(
            connection,
            FakePlanProvider(_history([100, 84, 101, 85.85])),
            end_date=date(2024, 1, 4),
        )

    status = result.statuses[0]
    assert status.evaluation.cycle_changed is True
    assert status.recorded_tier_keys == frozenset()
    assert [tier.key for tier in status.evaluation.newly_crossed_tiers] == ["0.15"]


def test_plan_readiness_is_derived_from_fee_and_even_closed_position(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        upsert_fund_fee(
            connection,
            fund_symbol="000001",
            fee_mode="rate",
            fee_value=0,
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=0,
            average_unit_cost=0,
        )
        result = read_drawdown_plan_statuses(
            connection,
            FakePlanProvider(_history([100, 90])),
            end_date=date(2024, 1, 2),
        )

    assert result.statuses[0].readiness == "READY"
    assert result.statuses[0].missing_setup == ()
    assert result.statuses[0].fund_nav is None


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


def test_manual_add_estimate_applies_exact_nav_once(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        params = json.loads(str(rule["params_json"]))
        params["tiers"][0]["drawdown"] = 0.151
        connection.execute(
            "UPDATE rules SET params_json = ? WHERE id = ?",
            (json.dumps(params), int(rule["id"])),
        )
        connection.commit()
        upsert_fund_fee(
            connection,
            fund_symbol="000001",
            fee_mode="rate",
            fee_value=0.01,
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=2,
        )
        confirmed = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        assert confirmed.notification is not None
        estimate_id, recorded = record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=confirmed.cycle_id,
            source_alert_event_id=confirmed.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(0.151, 5000, "0.151"),),
            action_at=datetime(2024, 1, 2, 10, tzinfo=UTC),
            create_estimate=True,
            cutoff_choice="before",
            effective_date="2024-01-02",
        )
        with pytest.raises(ValueError, match="does not match"):
            apply_manual_add_estimate(
                connection,
                estimate_id=estimate_id,
                nav=FundNav(
                    "000001",
                    date(2024, 1, 2),
                    float("nan"),
                    "akshare_eastmoney",
                ),
            )
        applied = apply_manual_add_estimate(
            connection,
            estimate_id=estimate_id,
            nav=FundNav("000001", date(2024, 1, 2), 2, "akshare_eastmoney"),
        )
        repeated = apply_manual_add_estimate(
            connection,
            estimate_id=estimate_id,
            nav=FundNav("000001", date(2024, 1, 2), 2, "akshare_eastmoney"),
        )
        position = get_position_snapshot(connection, "000001")
        actions = list_manual_add_actions(connection, confirmed.cycle_id)
        occurrence = connection.execute(
            "SELECT status, settlement_alert_event_id FROM manual_add_estimates"
        ).fetchone()

    assert recorded == ("0.151",)
    assert applied is not None
    assert repeated is None
    assert position["units"] == pytest.approx(100 + (5000 / 1.01) / 2)
    assert position["average_unit_cost"] == pytest.approx(
        5200 / (100 + (5000 / 1.01) / 2)
    )
    assert position["is_estimated"] == 1
    assert [row["tier_key"] for row in actions] == ["0.151"]
    assert "Configured tiers: -15.1%" in applied["message"]
    assert occurrence["status"] == "applied"
    assert occurrence["settlement_alert_event_id"] == applied["event_id"]


def test_position_sync_can_reconcile_pending_manual_estimate(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        upsert_fund_fee(
            connection,
            fund_symbol="000001",
            fee_mode="fixed",
            fee_value=1,
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=0,
            average_unit_cost=0,
        )
        confirmed = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        assert confirmed.notification is not None
        estimate_id, _recorded = record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=confirmed.cycle_id,
            source_alert_event_id=confirmed.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000, "0.15"),),
            action_at=datetime(2024, 1, 2, 10, tzinfo=UTC),
            create_estimate=True,
            cutoff_choice="before",
            effective_date="2024-01-02",
        )
        item_keys = tuple(
            str(item["key"])
            for item in list_pending_position_items(connection, "000001")
        )
        position = reconcile_position_snapshot(
            connection,
            fund_symbol="000001",
            units=2500,
            average_unit_cost=2,
            expected_item_keys=item_keys,
            included=True,
            synced_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
        occurrence = connection.execute(
            "SELECT status FROM manual_add_estimates WHERE id = ?",
            (estimate_id,),
        ).fetchone()

    assert item_keys == (f"estimate:{estimate_id}",)
    assert occurrence["status"] == "reconciled_by_sync"
    assert position["units"] == 2500
    assert position["is_estimated"] == 0


def test_manual_add_without_setup_requires_and_reconciles_position_sync(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        confirmed = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        assert confirmed.notification is not None
        estimate_id, recorded = record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=confirmed.cycle_id,
            source_alert_event_id=confirmed.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000, "0.15"),),
            action_at=datetime(2024, 1, 2, 10, tzinfo=UTC),
            create_estimate=False,
        )
        settings = get_fund_settings(connection, "000001")
        item_keys = tuple(
            str(item["key"])
            for item in list_pending_position_items(connection, "000001")
        )
        reconcile_position_snapshot(
            connection,
            fund_symbol="000001",
            units=2500,
            average_unit_cost=2,
            expected_item_keys=item_keys,
            included=True,
            synced_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
        remaining = list_pending_position_items(connection, "000001")
        reconciled_settings = get_fund_settings(connection, "000001")

    assert estimate_id is None
    assert recorded == ("0.15",)
    assert settings["position_sync_required_since"] is not None
    assert item_keys == (f"action:{confirmed.cycle_id}:0.15",)
    assert remaining == []
    assert reconciled_settings["position_sync_required_since"] is None


def test_unresolved_manual_add_blocks_later_position_estimate(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        upsert_fund_fee(
            connection,
            fund_symbol="000001",
            fee_mode="rate",
            fee_value=0.0015,
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=1000,
            average_unit_cost=1.2,
        )
        rule = list_rules(connection)[0]
        first = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=first.cycle_id,
            source_alert_event_id=first.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000, "0.15"),),
            action_at=datetime(2024, 1, 2, 10, tzinfo=UTC),
            create_estimate=False,
        )
        readiness = derive_plan_readiness(connection, "000001")
        second = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84, 79]),
            expected_date=date(2024, 1, 3),
        )

        with pytest.raises(sqlite3.IntegrityError, match="Position sync is required"):
            record_manual_addition(
                connection,
                rule_id=1,
                cycle_id=second.cycle_id,
                source_alert_event_id=second.notification.event_id,
                fund_symbol="000001",
                tiers=(DrawdownTier(0.20, 10000, "0.2"),),
                action_at=datetime(2024, 1, 3, 10, tzinfo=UTC),
                create_estimate=True,
                cutoff_choice="before",
                effective_date="2024-01-03",
            )

        actions = list_manual_add_actions(connection, first.cycle_id)
        estimate_count = connection.execute(
            "SELECT COUNT(*) FROM manual_add_estimates"
        ).fetchone()[0]

    assert [row["tier_key"] for row in actions] == ["0.15"]
    assert estimate_count == 0
    assert readiness == ("SETUP_REQUIRED", ("position sync required",))


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


def test_evaluation_selected_before_disable_cannot_persist(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    rule_id = _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        selected_rule = list_rules(connection)[0]
        assert delete_rule(connection, rule_id) is True

        with pytest.raises(sqlite3.IntegrityError, match="no longer enabled"):
            evaluate_drawdown_plan_rule(
                connection,
                selected_rule,
                _history([100, 84]),
                expected_date=date(2024, 1, 2),
            )

        cycle_count = connection.execute(
            "SELECT COUNT(*) FROM drawdown_cycles"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    assert cycle_count == 0
    assert event_count == 0


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
                expected_last_evaluated_date=None,
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


def test_stale_cycle_version_cannot_regress_evaluation_date(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    rule_id = _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        initial = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 90]),
            expected_date=date(2024, 1, 2),
        )
        persist_drawdown_plan_evaluation(
            connection,
            rule_id=rule_id,
            expected_active_cycle_id=initial.cycle_id,
            expected_last_evaluated_date="2024-01-02",
            start_new_cycle=False,
            peak_date="2024-01-01",
            peak_price=100,
            evaluation_date="2024-01-03",
        )

        with pytest.raises(sqlite3.IntegrityError, match="changed concurrently"):
            persist_drawdown_plan_evaluation(
                connection,
                rule_id=rule_id,
                expected_active_cycle_id=initial.cycle_id,
                expected_last_evaluated_date="2024-01-02",
                start_new_cycle=False,
                peak_date="2024-01-01",
                peak_price=100,
                evaluation_date="2024-01-02",
            )

        active = get_active_drawdown_cycle(connection, rule_id)

    assert active is not None
    assert active["last_evaluated_date"] == "2024-01-03"


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


class FakePlanProvider:
    def __init__(self, history: pd.DataFrame) -> None:
        self.history = history
        self.calls: list[tuple[Instrument, object, object, PriceBasis]] = []

    def get_history(
        self,
        instrument: Instrument,
        start_date: object,
        end_date: object,
        *,
        price_basis: PriceBasis = PriceBasis.UNADJUSTED,
    ) -> pd.DataFrame:
        self.calls.append((instrument, start_date, end_date, price_basis))
        return self.history
