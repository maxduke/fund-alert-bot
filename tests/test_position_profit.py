from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from fund_alert_bot import scheduler
from fund_alert_bot.checks import (
    evaluate_position_profit_rules,
    evaluate_profit_rules,
    reserve_drawdown_plan_data_unavailable_notice,
)
from fund_alert_bot.commands import CommandParseError, parse_add_profit_args
from fund_alert_bot.db import (
    add_alert_event,
    add_position_profit_rule,
    add_rule,
    delete_rule,
    get_active_position_cycle,
    init_db,
    list_position_profit_threshold_keys,
    list_retryable_drawdown_plan_alert_events,
    list_retryable_position_profit_alert_events,
    open_connection,
    upsert_position_snapshot,
)
from fund_alert_bot.market_data import FundNav


class OpenCalendar:
    def confirmed_status(self, check_date: date) -> bool:
        return check_date.weekday() < 5


class NavProvider:
    def __init__(self, nav: FundNav) -> None:
        self.nav = nav
        self.calls: list[tuple[str, date | None]] = []

    def get_fund_nav(self, instrument: object, nav_date: date | None = None) -> FundNav:
        self.calls.append((instrument.symbol, nav_date))
        return self.nav


class FailingBot:
    async def send_message(self, **kwargs: object) -> None:
        del kwargs
        raise RuntimeError("offline")


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> None:
        self.messages.append(dict(kwargs))


class LatestProvider:
    def get_latest(self, instrument: object) -> dict[str, object]:
        del instrument
        return {"date": "2024-01-02", "close": 1, "source": "test"}


def test_auto_profit_parser_is_strict() -> None:
    command = parse_add_profit_args(
        ["cn_open_fund", "000001", "A500 feeder", "auto", "20,30"]
    )
    assert command.cost == "auto"
    assert command.thresholds == [0.2, 0.3]

    for args in (
        ["cn_etf", "510500", "A500", "auto", "20,30"],
        ["cn_open_fund", "000001", "A500", "auto", "30,20"],
        ["cn_open_fund", "000001", "A500", "auto", "20,20"],
        ["cn_open_fund", "000001", "A500", "auto", "20,20.000000000001"],
        ["cn_open_fund", "000001", "A500", "auto", "nan"],
    ):
        with pytest.raises(CommandParseError):
            parse_add_profit_args(args)


def test_malformed_legacy_rule_does_not_abort_other_profit_rules(tmp_path) -> None:
    with open_connection(tmp_path / "fund-alert.sqlite3") as connection:
        init_db(connection)
        malformed_id = add_rule(
            connection,
            type="profit_reminder",
            symbol="510300",
            name="bad",
            asset_type="cn_etf",
            params={"cost": 1, "thresholds": [0.2]},
        )
        connection.execute(
            "UPDATE rules SET params_json = '[]' WHERE id = ?",
            (malformed_id,),
        )
        connection.commit()
        add_rule(
            connection,
            type="profit_reminder",
            symbol="159915",
            name="valid",
            asset_type="cn_etf",
            params={"cost": 1, "thresholds": [0.5]},
        )

        result = evaluate_profit_rules(connection, LatestProvider())
        assert result.checked_rules == 2
        assert len(result.errors) == 1
        assert result.errors[0].rule_id == malformed_id

        add_position_profit_rule(
            connection,
            fund_symbol="000001",
            name="A500 feeder",
            thresholds=(0.2,),
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
        position_result = evaluate_position_profit_rules(
            connection,
            NavProvider(FundNav("000001", date(2024, 1, 2), 1.1, "test")),
            OpenCalendar(),
            processing_date=date(2024, 1, 3),
        )

    assert position_result.checked_rules == 1
    assert len(position_result.errors) == 1
    assert position_result.errors[0].rule_id == malformed_id


def test_thresholds_are_once_per_continuous_positive_position_cycle(tmp_path) -> None:
    sqlite_path = tmp_path / "fund-alert.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_position_profit_rule(
            connection,
            fund_symbol="000001",
            name="A500 feeder",
            thresholds=(0.2, 0.3),
        )
        assert evaluate_profit_rules(connection, object()).checked_rules == 0
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
        first_cycle_id = int(get_active_position_cycle(connection, "000001")["id"])
        provider = NavProvider(
            FundNav("000001", date(2024, 1, 2), 1.35, "akshare_eastmoney")
        )

        first = evaluate_position_profit_rules(
            connection,
            provider,
            OpenCalendar(),
            processing_date=date(2024, 1, 3),
        )
        assert len(first.notifications) == 1
        assert "NAV source: akshare_eastmoney" in first.notifications[0].text
        assert "20.0%\n• 30.0%" in first.notifications[0].text
        assert list_position_profit_threshold_keys(
            connection,
            rule_id=rule_id,
            position_cycle_id=first_cycle_id,
        ) == {"0.2", "0.3"}

        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=120,
            average_unit_cost=1.1,
        )
        assert int(get_active_position_cycle(connection, "000001")["id"]) == (
            first_cycle_id
        )
        assert not evaluate_position_profit_rules(
            connection,
            provider,
            OpenCalendar(),
            processing_date=date(2024, 1, 3),
        ).notifications

        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=0,
            average_unit_cost=0,
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
        second_cycle_id = int(get_active_position_cycle(connection, "000001")["id"])
        assert second_cycle_id != first_cycle_id
        assert (
            len(
                evaluate_position_profit_rules(
                    connection,
                    provider,
                    OpenCalendar(),
                    processing_date=date(2024, 1, 3),
                ).notifications
            )
            == 1
        )
        assert provider.calls == [
            ("000001", date(2024, 1, 2)),
            ("000001", date(2024, 1, 2)),
        ]
        assert len(list_retryable_position_profit_alert_events(connection)) == 2


def test_stale_nav_and_missing_position_never_emit(tmp_path) -> None:
    with open_connection(tmp_path / "fund-alert.sqlite3") as connection:
        init_db(connection)
        add_position_profit_rule(
            connection,
            fund_symbol="000001",
            name="A500 feeder",
            thresholds=(0.2,),
        )
        missing = evaluate_position_profit_rules(
            connection,
            NavProvider(FundNav("000001", date(2024, 1, 2), 2, "akshare_eastmoney")),
            OpenCalendar(),
            processing_date=date(2024, 1, 3),
        )
        assert not missing.notifications
        assert not missing.no_data_skips

        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
        stale = evaluate_position_profit_rules(
            connection,
            NavProvider(FundNav("000001", date(2024, 1, 1), 2, "akshare_eastmoney")),
            OpenCalendar(),
            processing_date=date(2024, 1, 3),
        )
        assert not stale.notifications
        assert stale.no_data_skips
        assert stale.data_date == date(2024, 1, 2)
        assert (
            reserve_drawdown_plan_data_unavailable_notice(
                connection,
                evaluation_date=stale.data_date,
                result=stale,
                phase="fund_nav",
            )
            is not None
        )
        assert (
            reserve_drawdown_plan_data_unavailable_notice(
                connection,
                evaluation_date=stale.data_date,
                result=stale,
                phase="fund_nav",
            )
            is None
        )


def test_same_nav_date_is_not_replayed_after_cost_sync(tmp_path) -> None:
    with open_connection(tmp_path / "fund-alert.sqlite3") as connection:
        init_db(connection)
        add_position_profit_rule(
            connection,
            fund_symbol="000001",
            name="A500 feeder",
            thresholds=(0.2, 0.3),
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
        provider = NavProvider(
            FundNav("000001", date(2024, 1, 2), 1.25, "akshare_eastmoney")
        )
        assert (
            len(
                evaluate_position_profit_rules(
                    connection,
                    provider,
                    OpenCalendar(),
                    processing_date=date(2024, 1, 3),
                ).notifications
            )
            == 1
        )

        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=0.9,
        )
        assert not evaluate_position_profit_rules(
            connection,
            provider,
            OpenCalendar(),
            processing_date=date(2024, 1, 3),
        ).notifications
        assert len(provider.calls) == 1

        provider.nav = FundNav("000001", date(2024, 1, 3), 1.25, "akshare_eastmoney")
        assert (
            len(
                evaluate_position_profit_rules(
                    connection,
                    provider,
                    OpenCalendar(),
                    processing_date=date(2024, 1, 4),
                ).notifications
            )
            == 1
        )


def test_failed_delivery_retries_without_reopening_threshold(tmp_path) -> None:
    sqlite_path = tmp_path / "fund-alert.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_position_profit_rule(
            connection,
            fund_symbol="000001",
            name="A500 feeder",
            thresholds=(0.2,),
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
        cycle_id = int(get_active_position_cycle(connection, "000001")["id"])
        result = evaluate_position_profit_rules(
            connection,
            NavProvider(FundNav("000001", date(2024, 1, 2), 1.3, "akshare_eastmoney")),
            OpenCalendar(),
            processing_date=date(2024, 1, 3),
        )
        assert len(result.notifications) == 1

    assert (
        asyncio.run(
            scheduler.retry_pending_position_profit_notifications(
                application=SimpleNamespace(bot=FailingBot()),
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
            )
        )
        == 1
    )
    with open_connection(sqlite_path) as connection:
        assert delete_rule(connection, rule_id)
    bot = RecordingBot()
    assert (
        asyncio.run(
            scheduler.retry_pending_position_profit_notifications(
                application=SimpleNamespace(bot=bot),
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
            )
        )
        == 1
    )
    with open_connection(sqlite_path) as connection:
        assert (
            connection.execute(
                "SELECT notification_status FROM alert_events"
            ).fetchone()[0]
            == "sent"
        )
        assert list_position_profit_threshold_keys(
            connection,
            rule_id=rule_id,
            position_cycle_id=cycle_id,
        ) == {"0.2"}
    assert len(bot.messages) == 1
    assert bot.messages[0]["reply_markup"] is not None


def test_fund_nav_data_notice_uses_existing_retry_path(tmp_path) -> None:
    with open_connection(tmp_path / "fund-alert.sqlite3") as connection:
        init_db(connection)
        rule_id = add_position_profit_rule(
            connection,
            fund_symbol="000001",
            name="A500 feeder",
            thresholds=(0.2,),
        )
        event_id = add_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="data_unavailable:fund_nav:2024-01-03",
            title="Feeder-fund data unavailable",
            message="No exact NAV.",
            payload={"phase": "fund_nav", "data_date": "2024-01-03"},
        )

        assert [
            int(row["id"])
            for row in list_retryable_drawdown_plan_alert_events(connection)
        ] == [event_id]
