from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

import pytest

from fund_alert_bot.checks import (
    evaluate_dca_rules,
    process_scheduled_dca_occurrences,
)
from fund_alert_bot.db import (
    add_enhanced_dca_rule,
    connect,
    delete_rule,
    get_position_snapshot,
    get_scheduled_dca_occurrence,
    init_db,
    list_pending_position_items,
    reconcile_position_snapshot,
    skip_scheduled_dca_occurrence,
    upsert_fund_fee,
    upsert_position_snapshot,
)
from fund_alert_bot.market_data import FundNav, MarketCalendarUnavailableError


class Calendar:
    def __init__(self, open_dates: set[date]) -> None:
        self.open_dates = open_dates

    def confirmed_status(self, check_date: date) -> bool:
        return check_date in self.open_dates


class UnavailableCalendar:
    def confirmed_status(self, check_date: date) -> bool:
        raise MarketCalendarUnavailableError(f"calendar unavailable for {check_date}")


class Provider:
    def __init__(self, nav_date: date, value: float = 2) -> None:
        self.nav_date = nav_date
        self.value = value
        self.calls: list[tuple[str, date | None]] = []

    def get_fund_nav(self, instrument: Any, *, nav_date: date | None = None) -> FundNav:
        symbol = str(instrument.symbol)
        self.calls.append((symbol, nav_date))
        return FundNav(symbol, self.nav_date, self.value, "akshare_eastmoney")


def _add_rule(
    connection: object,
    *,
    weekday: str = "THU",
    policy: str = "next",
    fee_mode: str = "rate",
    fee_value: float = 0,
) -> int:
    return add_enhanced_dca_rule(
        connection,
        fund_symbol="110026",
        name="A500 feeder",
        weekday=weekday,
        amount=2000,
        fee_mode=fee_mode,
        fee_value=fee_value,
        holiday_policy=policy,
    )


def test_enhanced_dca_creates_occurrence_before_one_reminder() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    calendar = Calendar({date(2024, 1, 4)})

    first = evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )
    second = evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )
    occurrence = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")

    assert occurrence is not None
    assert occurrence["effective_date"] == "2024-01-04"
    assert occurrence["status"] == "pending"
    assert len(first.notifications) == 1
    assert first.notifications[0].telegram_actions[0][0][1] == ("dca_skip:1:2024-01-04")
    assert second.notifications == []


def test_holiday_next_applies_exact_next_open_nav_once() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    upsert_position_snapshot(
        connection,
        fund_symbol="110026",
        units=1000,
        average_unit_cost=1,
    )
    calendar = Calendar({date(2024, 1, 5)})
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )
    provider = Provider(date(2024, 1, 5), value=2)

    first = process_scheduled_dca_occurrences(
        connection,
        provider,
        calendar,
        processing_date=date(2024, 1, 6),
    )
    second = process_scheduled_dca_occurrences(
        connection,
        provider,
        calendar,
        processing_date=date(2024, 1, 6),
    )
    occurrence = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")
    position = get_position_snapshot(connection, "110026")

    assert first.errors == []
    assert second.checked_estimates == 0
    assert provider.calls == [("110026", date(2024, 1, 5))]
    assert occurrence["status"] == "applied"
    assert occurrence["nav_date"] == "2024-01-05"
    assert occurrence["added_units"] == pytest.approx(1000)
    assert position["units"] == pytest.approx(2000)
    assert position["average_unit_cost"] == pytest.approx(1.5)
    assert position["is_estimated"] == 1


def test_holiday_skip_never_fetches_nav_or_changes_position() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection, policy="skip")
    upsert_position_snapshot(
        connection,
        fund_symbol="110026",
        units=1000,
        average_unit_cost=1,
    )
    calendar = Calendar(set())

    result = evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )
    provider = Provider(date(2024, 1, 5))
    process_scheduled_dca_occurrences(
        connection,
        provider,
        calendar,
        processing_date=date(2024, 1, 6),
    )
    occurrence = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")
    position = get_position_snapshot(connection, "110026")

    assert occurrence["status"] == "skipped"
    assert result.notifications[0].telegram_actions == ()
    assert provider.calls == []
    assert position["units"] == 1000


def test_missing_position_leaves_occurrence_pending_without_nav_request() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    calendar = Calendar({date(2024, 1, 4)})
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )
    provider = Provider(date(2024, 1, 4))

    process_scheduled_dca_occurrences(
        connection,
        provider,
        calendar,
        processing_date=date(2024, 1, 5),
    )

    assert provider.calls == []
    assert (
        get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")["status"]
        == "pending"
    )


def test_position_sync_atomically_reconciles_pending_dca() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=Calendar({date(2024, 1, 4)}),
    )
    keys = tuple(
        str(item["key"]) for item in list_pending_position_items(connection, "110026")
    )

    reconcile_position_snapshot(
        connection,
        fund_symbol="110026",
        units=100,
        average_unit_cost=2,
        expected_item_keys=keys,
        all_included=True,
        synced_at="2024-01-05T00:00:00+00:00",
    )

    assert keys == ("dca:1",)
    assert (
        get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")["status"]
        == "reconciled_by_sync"
    )


def test_duplicate_rule_and_late_skip_are_safe() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    with pytest.raises(sqlite3.IntegrityError, match="already uses"):
        _add_rule(connection)
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=Calendar({date(2024, 1, 4)}),
    )
    assert (
        skip_scheduled_dca_occurrence(
            connection,
            rule_id=rule_id,
            due_date="2024-01-04",
        )
        == "skipped"
    )
    assert (
        skip_scheduled_dca_occurrence(
            connection,
            rule_id=rule_id,
            due_date="2024-01-04",
        )
        == "skipped"
    )


def test_same_fund_and_nav_date_are_fetched_once_per_processing_run() -> None:
    connection = connect(":memory:")
    init_db(connection)
    _add_rule(connection, weekday="MON")
    _add_rule(connection, weekday="TUE")
    upsert_position_snapshot(
        connection,
        fund_symbol="110026",
        units=0,
        average_unit_cost=0,
    )
    calendar = Calendar({date(2024, 1, 3)})
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 1),
        market_calendar=calendar,
    )
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 2),
        market_calendar=calendar,
    )
    provider = Provider(date(2024, 1, 3))
    nav_cache: dict[tuple[str, date], object] = {}
    nav_errors: dict[tuple[str, date], Exception] = {}

    result = process_scheduled_dca_occurrences(
        connection,
        provider,
        calendar,
        processing_date=date(2024, 1, 4),
        nav_cache=nav_cache,
        nav_errors=nav_errors,
    )

    assert result.errors == []
    assert provider.calls == [("110026", date(2024, 1, 3))]
    assert get_position_snapshot(connection, "110026")["estimates_since_sync"] == 2


def test_wrong_nav_date_leaves_occurrence_pending() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    upsert_position_snapshot(
        connection,
        fund_symbol="110026",
        units=100,
        average_unit_cost=1,
    )
    calendar = Calendar({date(2024, 1, 4)})
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )

    result = process_scheduled_dca_occurrences(
        connection,
        Provider(date(2024, 1, 3)),
        calendar,
        processing_date=date(2024, 1, 5),
    )

    assert "does not match" in result.errors[0].message
    assert (
        get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")["status"]
        == "pending"
    )
    assert get_position_snapshot(connection, "110026")["units"] == 100


def test_calendar_failure_keeps_occurrence_pending_without_nav() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    upsert_position_snapshot(
        connection,
        fund_symbol="110026",
        units=100,
        average_unit_cost=1,
    )
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=UnavailableCalendar(),
    )
    provider = Provider(date(2024, 1, 4))

    result = process_scheduled_dca_occurrences(
        connection,
        provider,
        UnavailableCalendar(),
        processing_date=date(2024, 1, 5),
    )

    assert len(result.no_data_skips) == 1
    assert provider.calls == []
    assert (
        get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")["status"]
        == "pending"
    )


def test_pending_occurrence_survives_restart_and_resumes(tmp_path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with connect(sqlite_path) as connection:
        init_db(connection)
        rule_id = _add_rule(connection)
        upsert_position_snapshot(
            connection,
            fund_symbol="110026",
            units=0,
            average_unit_cost=0,
        )
        evaluate_dca_rules(
            connection,
            today=date(2024, 1, 4),
            market_calendar=Calendar({date(2024, 1, 4)}),
        )

    with connect(sqlite_path) as connection:
        init_db(connection)
        process_scheduled_dca_occurrences(
            connection,
            Provider(date(2024, 1, 4)),
            Calendar({date(2024, 1, 4)}),
            processing_date=date(2024, 1, 5),
        )
        occurrence = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")

    assert occurrence["status"] == "applied"


def test_disabling_fixed_dca_preserves_pending_occurrence() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection)
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=Calendar({date(2024, 1, 4)}),
    )

    assert delete_rule(connection, rule_id)

    rule = connection.execute(
        "SELECT enabled FROM rules WHERE id = ?", (rule_id,)
    ).fetchone()
    occurrence = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")
    assert rule["enabled"] == 0
    assert occurrence["status"] == "pending"


def test_fee_change_affects_only_future_occurrences() -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(connection, fee_mode="rate", fee_value=0.001)
    calendar = Calendar({date(2024, 1, 4), date(2024, 1, 11)})
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )
    upsert_fund_fee(
        connection,
        fund_symbol="110026",
        fee_mode="fixed",
        fee_value=1,
    )
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 11),
        market_calendar=calendar,
    )

    first = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")
    second = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-11")
    assert (first["fee_mode"], first["fee_value"]) == ("rate", 0.001)
    assert (second["fee_mode"], second["fee_value"]) == ("fixed", 1)


@pytest.mark.parametrize(
    ("fee_mode", "fee_value", "expected_units"),
    [
        ("rate", 0.001, (2000 / 1.001) / 2),
        ("fixed", 1, 1999 / 2),
    ],
)
def test_scheduled_dca_fee_formulas(
    fee_mode: str,
    fee_value: float,
    expected_units: float,
) -> None:
    connection = connect(":memory:")
    init_db(connection)
    rule_id = _add_rule(
        connection,
        fee_mode=fee_mode,
        fee_value=fee_value,
    )
    upsert_position_snapshot(
        connection,
        fund_symbol="110026",
        units=0,
        average_unit_cost=0,
    )
    calendar = Calendar({date(2024, 1, 4)})
    evaluate_dca_rules(
        connection,
        today=date(2024, 1, 4),
        market_calendar=calendar,
    )
    process_scheduled_dca_occurrences(
        connection,
        Provider(date(2024, 1, 4), value=2),
        calendar,
        processing_date=date(2024, 1, 5),
    )

    occurrence = get_scheduled_dca_occurrence(connection, rule_id, "2024-01-04")
    assert occurrence["added_units"] == pytest.approx(expected_units)
