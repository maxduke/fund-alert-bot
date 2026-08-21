from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from fund_alert_bot.checks import (
    derive_plan_readiness,
    evaluate_drawdown_plan_rule,
    evaluate_drawdown_plan_rules,
    get_cached_or_fetch_fund_nav,
    process_manual_add_estimates,
    read_drawdown_plan_statuses,
)
from fund_alert_bot.db import (
    add_drawdown_plan_pre_alert_event,
    add_rule,
    apply_manual_add_estimate,
    delete_rule,
    get_active_drawdown_cycle,
    get_cached_fund_nav,
    get_drawdown_tier_reminder_states,
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
    snooze_drawdown_tiers_for_date,
    upsert_fund_fee,
    upsert_fund_nav,
    upsert_position_snapshot,
)
from fund_alert_bot.market_data import (
    AssetType,
    FundNav,
    Instrument,
    PriceBasis,
)
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


def test_market_tier_facts_can_persist_without_an_alert(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    rule_id = _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        cycle_id, event_id = persist_drawdown_plan_evaluation(
            connection,
            rule_id=rule_id,
            expected_active_cycle_id=None,
            expected_last_evaluated_date=None,
            start_new_cycle=True,
            peak_date="2024-01-01",
            peak_price=100,
            evaluation_date="2024-01-02",
            tiers_to_record=(DrawdownTier(0.15, 5000, "0.15"),),
        )
        records = list_drawdown_tier_records(connection, cycle_id)
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    assert event_id is None
    assert [row["tier_key"] for row in records] == ["0.15"]
    assert records[0]["alert_event_id"] is None
    assert event_count == 0


def test_prealert_add_is_upgraded_to_close_confirmed_fact(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        initial = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100]),
            expected_date=date(2024, 1, 1),
        )
        tier = DrawdownTier(0.15, 5000, "0.15")
        prealert_id = add_drawdown_plan_pre_alert_event(
            connection,
            rule_id=1,
            alert={
                "alert_key": "pre-alert-test",
                "title": "pre-alert",
                "message": "pre-alert",
                "payload": {
                    "cycle_id": initial.cycle_id,
                    "data_date": "2024-01-02",
                    "investment_fund_symbol": "000001",
                    "crossed_tiers": [
                        {
                            "key": tier.key,
                            "drawdown": tier.drawdown,
                            "amount": tier.amount,
                        }
                    ],
                },
            },
        )
        record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=initial.cycle_id,
            source_alert_event_id=prealert_id,
            fund_symbol="000001",
            tiers=(tier,),
            action_at=datetime(2024, 1, 2, 10, tzinfo=UTC),
            create_estimate=False,
        )
        result = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        records = list_drawdown_tier_records(connection, result.cycle_id)

    assert [(row["tier_key"], row["source"]) for row in records] == [
        ("0.15", "close_confirmed")
    ]


def test_suppressed_market_fact_has_no_unrelated_alert_link(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        first = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100]),
            expected_date=date(2024, 1, 1),
        )
        snooze_drawdown_tiers_for_date(
            connection,
            cycle_id=first.cycle_id,
            tier_keys=("0.15",),
            market_date="2024-01-02",
        )
        result = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 74]),
            expected_date=date(2024, 1, 2),
        )
        records = list_drawdown_tier_records(connection, result.cycle_id)
        event = connection.execute(
            "SELECT id FROM alert_events ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert event is not None
    assert {row["tier_key"]: row["alert_event_id"] for row in records} == {
        "0.15": None,
        "0.2": event["id"],
        "0.25": event["id"],
    }


def test_pending_tier_follow_up_is_one_daily_action_event(tmp_path: Path) -> None:
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
            _history([100, 84, 83]),
            expected_date=date(2024, 1, 3),
        )
        events = connection.execute(
            "SELECT alert_key, payload_json FROM alert_events ORDER BY id"
        ).fetchall()
        records = list_drawdown_tier_records(connection, second.cycle_id)

    assert first.notification is not None
    assert second.notification is not None
    assert len(events) == 2
    assert events[1]["alert_key"] == (
        f"{rule_id}:drawdown_plan:action:{second.cycle_id}:2024-01-03"
    )
    payload = json.loads(events[1]["payload_json"])
    assert payload["newly_crossed_tiers"] == []
    assert payload["pending_tiers"] == [
        {"key": "0.15", "drawdown": 0.15, "amount": 5000}
    ]
    assert payload["actionable_tiers"] == payload["pending_tiers"]
    assert [row["tier_key"] for row in records] == ["0.15"]


def test_snoozed_close_records_fact_but_suppresses_same_day_alert(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        first = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100]),
            expected_date=date(2024, 1, 1),
        )
        assert first.notification is None
        cycle = get_active_drawdown_cycle(connection, 1)
        assert cycle is not None
        snooze_drawdown_tiers_for_date(
            connection,
            cycle_id=int(cycle["id"]),
            tier_keys=("0.15",),
            market_date="2024-01-02",
        )
        second = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        records = list_drawdown_tier_records(connection, second.cycle_id)
        states = get_drawdown_tier_reminder_states(connection, second.cycle_id)
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    assert second.notification is None
    assert [row["tier_key"] for row in records] == ["0.15"]
    assert states["0.15"]["snoozed_market_date"] == "2024-01-02"
    assert event_count == 0


def test_snoozed_fact_gets_provenance_from_later_action_alert(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        first = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100]),
            expected_date=date(2024, 1, 1),
        )
        snooze_drawdown_tiers_for_date(
            connection,
            cycle_id=first.cycle_id,
            tier_keys=("0.15",),
            market_date="2024-01-02",
        )
        evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84]),
            expected_date=date(2024, 1, 2),
        )
        later = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84, 83]),
            expected_date=date(2024, 1, 3),
        )
        assert later.notification is not None
        record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=later.cycle_id,
            source_alert_event_id=later.notification.event_id,
            source_alert_event_ids={"0.15": later.notification.event_id},
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000, "0.15"),),
            action_at=datetime(2024, 1, 3, 10, tzinfo=UTC),
            action_date_override=date(2024, 1, 2),
            create_estimate=False,
        )
        records = list_drawdown_tier_records(connection, later.cycle_id)
        actions = list_manual_add_actions(connection, later.cycle_id)

    assert [row["tier_key"] for row in records] == ["0.15"]
    assert records[0]["alert_event_id"] == later.notification.event_id
    assert records[0]["source"] == "close_confirmed"
    assert records[0]["data_date"] == "2024-01-02"
    assert [(row["tier_key"], row["source_alert_event_id"]) for row in actions] == [
        ("0.15", later.notification.event_id)
    ]


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


def test_read_plan_status_is_pure_and_uses_confirmed_history(tmp_path: Path) -> None:
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
    assert status.evaluation.drawdown == pytest.approx(0.0)
    assert status.recorded_tier_keys == frozenset()
    assert status.readiness == "SETUP_REQUIRED"
    assert status.missing_setup == ("fund fee", "position snapshot")
    assert counts == [0, 0, 0]


def test_read_plan_status_reuses_persisted_history_until_refresh(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    provider = FakePlanProvider(_history([100, 80]))

    with open_connection(sqlite_path) as connection:
        first = read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 2),
        )
        second = read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 2),
        )
        refreshed = read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 2),
            force_refresh=True,
        )

    assert len(first.statuses) == len(second.statuses) == len(refreshed.statuses) == 1
    # The status path does not persist the requested session date as a
    # confirmed close, so a short fixture without a covered prefix is fetched
    # again until the explicit refresh. Production history normally covers the
    # full required range and therefore reuses the cache.
    assert len(provider.calls) == 3


def test_read_plan_status_uses_confirmed_history_for_nontrading_end_date(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    provider = FakePlanProvider(_long_history())

    with open_connection(sqlite_path) as connection:
        first = read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 7),
        )
        second = read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 7),
        )

    assert first.statuses[0].evaluation.latest_date == date(2024, 1, 6)
    assert second.statuses[0].evaluation.latest_date == date(2024, 1, 6)
    assert len(provider.calls) == 1
    assert provider.calls[0][1] == date(2022, 7, 3)


def test_cached_fund_nav_is_used_for_read_only_plan_status(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    provider = FakePlanProvider(_history([100, 80]))
    nav_calls: list[str] = []

    def get_fund_nav(instrument: Instrument, **kwargs: object) -> FundNav:
        del kwargs
        nav_calls.append(instrument.symbol)
        return FundNav(instrument.symbol, date(2024, 1, 2), 2, "akshare_eastmoney")

    provider.get_fund_nav = get_fund_nav  # type: ignore[method-assign]
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
            units=100,
            average_unit_cost=1,
        )
        read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 2),
        )
        read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 2),
        )

    assert nav_calls == ["000001"]


def test_latest_cached_fund_nav_is_reused_when_it_meets_minimum_date(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    provider = FakePlanProvider(_history([100, 80]))
    nav_calls: list[str] = []

    def get_fund_nav(instrument: Instrument, **kwargs: object) -> FundNav:
        del kwargs
        nav_calls.append(instrument.symbol)
        return FundNav(instrument.symbol, date(2024, 1, 3), 2, "provider")

    provider.get_fund_nav = get_fund_nav  # type: ignore[method-assign]
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        upsert_fund_nav(
            connection,
            fund_symbol="000001",
            nav_date=date(2024, 1, 3),
            unit_nav=2,
            source="cached",
        )
        nav = get_cached_or_fetch_fund_nav(
            connection,
            provider,
            "000001",
            minimum_date=date(2024, 1, 2),
        )

    assert nav.date == date(2024, 1, 3)
    assert nav.source == "cached"
    assert nav_calls == []


def test_stale_latest_cached_fund_nav_is_refreshed_and_persisted(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    provider = FakePlanProvider(_history([100, 80]))
    provider.get_fund_nav = lambda instrument, **kwargs: FundNav(  # type: ignore[method-assign]
        instrument.symbol,
        date(2024, 1, 3),
        2.5,
        "provider",
    )
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        upsert_fund_nav(
            connection,
            fund_symbol="000001",
            nav_date=date(2024, 1, 1),
            unit_nav=2,
            source="cached",
        )
        nav = get_cached_or_fetch_fund_nav(
            connection,
            provider,
            "000001",
            minimum_date=date(2024, 1, 3),
        )
        cached = get_cached_fund_nav(connection, "000001")

    assert nav.date == date(2024, 1, 3)
    assert nav.value == 2.5
    assert cached is not None
    assert cached["nav_date"] == "2024-01-03"


def test_stale_provider_nav_skips_plan_valuation(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    _add_plan(sqlite_path)
    provider = FakePlanProvider(_history([100, 80]))
    provider.get_fund_nav = lambda instrument, **kwargs: FundNav(  # type: ignore[method-assign]
        instrument.symbol,
        date(2024, 1, 1),
        2,
        "provider",
    )
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
            units=100,
            average_unit_cost=1,
        )
        result = read_drawdown_plan_statuses(
            connection,
            provider,
            end_date=date(2024, 1, 2),
            minimum_fund_nav_date=date(2024, 1, 2),
        )

    assert len(result.statuses) == 1
    assert result.statuses[0].fund_nav is None
    assert len(result.no_data_skips) == 1
    assert "required at least 2024-01-02" in result.no_data_skips[0].message


def test_force_refresh_ignores_sufficiently_recent_latest_nav(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    provider = FakePlanProvider(_history([100, 80]))
    nav_calls: list[str] = []

    def get_fund_nav(instrument: Instrument, **kwargs: object) -> FundNav:
        del kwargs
        nav_calls.append(instrument.symbol)
        return FundNav(instrument.symbol, date(2024, 1, 4), 3, "provider")

    provider.get_fund_nav = get_fund_nav  # type: ignore[method-assign]
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        upsert_fund_nav(
            connection,
            fund_symbol="000001",
            nav_date=date(2024, 1, 3),
            unit_nav=2,
            source="cached",
        )
        nav = get_cached_or_fetch_fund_nav(
            connection,
            provider,
            "000001",
            minimum_date=date(2024, 1, 2),
            force_refresh=True,
        )

    assert nav.date == date(2024, 1, 4)
    assert nav_calls == ["000001"]


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
    assert status.evaluation.latest_date == date(2024, 1, 3)
    assert status.evaluation.drawdown == pytest.approx(0.0)
    assert status.evaluation.newly_crossed_tiers == ()


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
        params["tiers"][0]["amount"] = 5000.005
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
            tiers=(DrawdownTier(0.151, 5000.005, "0.151"),),
            action_at=datetime(2024, 1, 2, 10, tzinfo=UTC),
            create_estimate=True,
            cutoff_time="15:00",
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
    assert position["units"] == pytest.approx(100 + (5000.005 / 1.01) / 2)
    assert position["average_unit_cost"] == pytest.approx(
        5200.005 / (100 + (5000.005 / 1.01) / 2)
    )
    assert position["is_estimated"] == 1
    assert [row["tier_key"] for row in actions] == ["0.151"]
    assert "Configured tiers: -15.1%" in applied["message"]
    assert "Gross amount: ¥5,000.005" in applied["message"]
    assert "Captured subscription fee: rate:1%" in applied["message"]
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
            cutoff_time="15:00",
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
            all_included=True,
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
            all_included=True,
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
                cutoff_time="15:00",
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


def test_position_sync_marker_defers_older_pending_estimate(tmp_path: Path) -> None:
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
        estimate_id, _recorded = record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=first.cycle_id,
            source_alert_event_id=first.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000, "0.15"),),
            action_at=datetime(2024, 1, 2, 10, tzinfo=UTC),
            create_estimate=True,
            cutoff_time="15:00",
            cutoff_choice="before",
            effective_date="2024-01-02",
        )
        second = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _history([100, 84, 79]),
            expected_date=date(2024, 1, 3),
        )
        record_manual_addition(
            connection,
            rule_id=1,
            cycle_id=second.cycle_id,
            source_alert_event_id=second.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(0.20, 10000, "0.2"),),
            action_at=datetime(2024, 1, 3, 10, tzinfo=UTC),
            create_estimate=False,
        )
        nav_calls: list[str] = []
        result = process_manual_add_estimates(
            connection,
            SimpleNamespace(
                get_fund_nav=lambda instrument, **kwargs: nav_calls.append(
                    instrument.symbol
                )
            ),
            SimpleNamespace(confirmed_status=lambda check_date: True),
            processing_date=date(2024, 1, 3),
        )
        with pytest.raises(sqlite3.IntegrityError, match="Position sync is required"):
            apply_manual_add_estimate(
                connection,
                estimate_id=estimate_id,
                nav=FundNav("000001", date(2024, 1, 2), 2, "akshare_eastmoney"),
            )
        position = get_position_snapshot(connection, "000001")
        occurrence = connection.execute(
            "SELECT status FROM manual_add_estimates WHERE id = ?",
            (estimate_id,),
        ).fetchone()

    assert nav_calls == []
    assert result.notifications == []
    assert result.errors == []
    assert (position["units"], position["average_unit_cost"]) == (1000, 1.2)
    assert occurrence["status"] == "pending"


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


def _long_history() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2022-06-20", "2024-01-07", freq="D"),
            "close": 100.0,
            "source": "akshare_eastmoney",
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
