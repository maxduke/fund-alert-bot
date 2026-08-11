from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from fund_alert_bot import commands, scheduler
from fund_alert_bot.checks import evaluate_drawdown_plan_rule
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import (
    add_rule,
    get_position_snapshot,
    initialize_database,
    list_rules,
    open_connection,
    record_manual_addition,
    upsert_fund_fee,
    upsert_position_snapshot,
)
from fund_alert_bot.market_data import (
    AssetType,
    FundNav,
    Instrument,
    MarketCalendarUnavailableError,
    PriceBasis,
    RealtimeQuote,
)
from fund_alert_bot.rules.drawdown_plan import DrawdownTier

EXPECTED_DRAWDOWN_10_MESSAGE = "\n".join(
    (
        "📉 Drawdown reminder",
        "",
        "• Symbol: 399006",
        "• Name: ChiNext Index",
        "• Asset type: cn_index",
        "• Lookback: 365 days",
        "• Drawdown: 10.0%",
        "• Triggered threshold: 10.0%",
        "• Peak: 100 on 2024-01-01",
        "• Latest: 90 on 2024-01-02",
        "",
        "Reminder: this is not automatic trading and no orders will be placed.",
    )
)

EXPECTED_DRAWDOWN_11_MESSAGE = "\n".join(
    (
        "📉 Drawdown reminder",
        "",
        "• Symbol: 399006",
        "• Name: ChiNext Index",
        "• Asset type: cn_index",
        "• Lookback: 365 days",
        "• Drawdown: 11.0%",
        "• Triggered threshold: 10.0%",
        "• Peak: 100 on 2024-01-01",
        "• Latest: 89 on 2024-01-02",
        "",
        "Reminder: this is not automatic trading and no orders will be placed.",
    )
)

EXPECTED_DCA_MESSAGE = "\n".join(
    (
        "💰 DCA reminder",
        "",
        "• 标的：创业板",
        "• 日期：2024-01-04",
        "• 计划金额：1000 元",
        "",
        "提醒：这是纪律提醒，不会自动交易。",
    )
)

EXPECTED_PROFIT_MESSAGE = "\n".join(
    (
        "💰 Price-Gain reminder",
        "",
        "• Symbol: 159915",
        "• Name: ChiNext ETF",
        "• Asset type: cn_etf",
        "• Cost: 1.85",
        "• Latest price: 2.4",
        "• Profit rate: 29.7%",
        "• Triggered threshold: 25.0%",
        "",
        "This is a price-gain reminder only.",
        "No trade has been placed.",
    )
)


def test_scheduler_time_parsing() -> None:
    parsed_time = scheduler.parse_after_close_check_time("17:10")

    assert parsed_time.hour == 17
    assert parsed_time.minute == 10


def test_dca_reminder_time_parsing() -> None:
    parsed_time = scheduler.parse_dca_reminder_time("09:30")

    assert parsed_time.hour == 9
    assert parsed_time.minute == 30


def test_fund_nav_process_time_parsing() -> None:
    parsed_time = scheduler.parse_fund_nav_process_time("08:30")

    assert (parsed_time.hour, parsed_time.minute) == (8, 30)


@pytest.mark.parametrize("raw_value", ["", "1710", "24:00", "17:60", "aa:10"])
def test_scheduler_time_parsing_rejects_invalid_values(raw_value: str) -> None:
    with pytest.raises(ValueError, match="AFTER_CLOSE_CHECK_TIME"):
        scheduler.parse_after_close_check_time(raw_value)


def test_weekday_trigger_skips_weekends() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    trigger = scheduler.create_weekday_after_close_trigger(
        check_time=scheduler.parse_after_close_check_time("17:10"),
        timezone=timezone,
    )

    next_fire = trigger.get_next_fire_time(
        None,
        datetime(2024, 1, 5, 17, 11, tzinfo=timezone),
    )

    assert next_fire is not None
    assert next_fire.date() == date(2024, 1, 8)
    assert next_fire.weekday() == 0
    assert next_fire.hour == 17
    assert next_fire.minute == 10


def test_daily_dca_trigger_runs_on_weekends() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    trigger = scheduler.create_daily_dca_trigger(
        reminder_time=scheduler.parse_dca_reminder_time("09:30"),
        timezone=timezone,
    )

    next_fire = trigger.get_next_fire_time(
        None,
        datetime(2024, 1, 5, 9, 31, tzinfo=timezone),
    )

    assert next_fire is not None
    assert next_fire.date() == date(2024, 1, 6)
    assert next_fire.weekday() == 5
    assert next_fire.hour == 9
    assert next_fire.minute == 30


def test_check_and_scheduler_use_same_evaluator() -> None:
    assert commands.evaluate_drawdown_rules is scheduler.evaluate_drawdown_rules
    assert commands.evaluate_dca_rules is scheduler.evaluate_dca_rules
    assert commands.evaluate_profit_rules is scheduler.evaluate_profit_rules


def test_scheduled_check_prevents_duplicate_alerts_by_alert_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_history(["2024-01-01", "2024-01-02"], [100.0, 90.0]))
    market_calendar = FakeMarketCalendar(is_trading_day=True)
    webhook_calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> object:
        webhook_calls.append({"url": url, **kwargs})
        return FakeResponse(status_code=200)

    monkeypatch.setattr(
        "fund_alert_bot.notifications.webhook.requests.post",
        fake_post,
    )

    for _ in range(2):
        asyncio.run(
            scheduler.run_scheduled_drawdown_check(
                application=application,
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
                market_data_provider=provider,
                market_calendar=market_calendar,
                timezone="Asia/Shanghai",
                run_date=date(2024, 1, 2),
                notification_settings=NotificationSettings(
                    webhook_enabled=True,
                    webhook_url="https://hooks.example.test/secret",
                ),
            )
        )

    with open_connection(sqlite_path) as connection:
        event_row = connection.execute(
            """
            SELECT notification_status
            FROM alert_events
            """
        ).fetchone()

    assert event_row["notification_status"] == "sent"
    assert application.bot.messages == [
        {"chat_id": 123, "text": EXPECTED_DRAWDOWN_10_MESSAGE}
    ]
    assert webhook_calls == [
        {
            "url": "https://hooks.example.test/secret",
            "json": {
                "title": "Drawdown reminder",
                "body": EXPECTED_DRAWDOWN_10_MESSAGE,
            },
            "timeout": 10,
        }
    ]


def test_scheduled_check_logs_and_skips_when_no_new_data(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_history(["2024-01-01"], [100.0]))
    market_calendar = FakeMarketCalendar(is_trading_day=True)
    caplog.set_level(logging.INFO, logger="fund_alert_bot.scheduler")

    asyncio.run(
        scheduler.run_scheduled_drawdown_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=market_calendar,
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )

    with open_connection(sqlite_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    assert event_count == 0
    assert application.bot.messages == []
    assert "Scheduled market reminder check started" in caplog.text
    assert "Scheduled market reminder check skipped" in caplog.text
    assert "No market data available for 2024-01-02" in caplog.text
    assert "Scheduled market reminder check ended" in caplog.text


def test_scheduled_drawdown_check_skips_when_cn_market_is_closed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_history(["2024-04-30"], [100.0]))
    market_calendar = FakeMarketCalendar(is_trading_day=False)
    caplog.set_level(logging.INFO, logger="fund_alert_bot.scheduler")

    asyncio.run(
        scheduler.run_scheduled_drawdown_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=market_calendar,
            timezone="Asia/Shanghai",
            run_date=date(2024, 5, 1),
        )
    )

    assert provider.calls == []
    assert application.bot.messages == []
    assert market_calendar.checked_dates == [date(2024, 5, 1)]
    assert "CN market is not trading" in caplog.text


def test_register_jobs_passes_calendar_to_calendar_aware_jobs() -> None:
    fake_scheduler = FakeScheduler()
    application = FakeApplication()
    provider = FakeProvider(_history(["2024-01-02"], [100.0]))
    market_calendar = FakeMarketCalendar(is_trading_day=True)

    scheduler.register_jobs(
        fake_scheduler,
        application=application,
        sqlite_path=":memory:",
        allowed_user_ids={123},
        timezone="Asia/Shanghai",
        market_data_provider=provider,
        market_calendar=market_calendar,
    )

    before_close_job = fake_scheduler.jobs[scheduler.MARKET_BEFORE_CLOSE_JOB_ID]
    after_close_job = fake_scheduler.jobs[scheduler.MARKET_AFTER_CLOSE_JOB_ID]
    fund_nav_job = fake_scheduler.jobs[scheduler.FUND_NAV_PROCESS_JOB_ID]
    dca_job = fake_scheduler.jobs[scheduler.DCA_MORNING_JOB_ID]

    assert before_close_job["func"] is scheduler.run_scheduled_before_close_check
    assert before_close_job["kwargs"]["market_calendar"] is market_calendar
    assert before_close_job["kwargs"]["market_data_provider"] is provider
    assert after_close_job["func"] is scheduler.run_scheduled_market_check
    assert after_close_job["kwargs"]["market_calendar"] is market_calendar
    assert after_close_job["kwargs"]["market_data_provider"] is provider
    assert fund_nav_job["func"] is scheduler.run_scheduled_fund_nav_process
    assert fund_nav_job["kwargs"]["market_calendar"] is market_calendar
    assert fund_nav_job["kwargs"]["market_data_provider"] is provider
    assert dca_job["kwargs"]["market_calendar"] is market_calendar


def test_scheduled_before_close_check_uses_latest_drawdown_price(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(
        _history(["2024-01-01", "2024-01-02"], [100.0, 95.0]),
        latest={"date": "2024-01-02", "close": 89.0, "source": "test"},
    )
    market_calendar = FakeMarketCalendar(is_trading_day=True)

    asyncio.run(
        scheduler.run_scheduled_before_close_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=market_calendar,
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )

    assert [call.asset_type for call in provider.latest_calls] == [AssetType.CN_INDEX]
    assert application.bot.messages == [
        {"chat_id": 123, "text": EXPECTED_DRAWDOWN_11_MESSAGE}
    ]


def test_scheduled_before_close_check_skips_stale_latest_data(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(
        _history(["2024-01-01"], [90.0]),
        latest={"date": "2024-01-01", "close": 89.0, "source": "test"},
    )
    market_calendar = FakeMarketCalendar(is_trading_day=True)

    asyncio.run(
        scheduler.run_scheduled_before_close_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=market_calendar,
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )

    assert [call.asset_type for call in provider.latest_calls] == [AssetType.CN_INDEX]
    assert application.bot.messages == []


def test_scheduled_before_close_plan_prealert_does_not_consume_tiers(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(
        _plan_history([100, 80]),
        realtime_quote=RealtimeQuote(
            symbol="510300",
            price=79,
            previous_close=80,
            volume=100,
            amount=1000,
            source="eastmoney",
            fetched_at=datetime(2024, 1, 3, 6, 50, tzinfo=ZoneInfo("UTC")),
        ),
    )
    market_calendar = FakeMarketCalendar(is_trading_day=True)

    for _ in range(2):
        asyncio.run(
            scheduler.run_scheduled_before_close_check(
                application=application,
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
                market_data_provider=provider,
                market_calendar=market_calendar,
                timezone="Asia/Shanghai",
                run_date=date(2024, 1, 3),
            )
        )

    with open_connection(sqlite_path) as connection:
        event = connection.execute(
            "SELECT alert_key, notification_status FROM alert_events"
        ).fetchone()
        cycle_count = connection.execute(
            "SELECT COUNT(*) FROM drawdown_cycles"
        ).fetchone()[0]
        tier_count = connection.execute(
            "SELECT COUNT(*) FROM drawdown_tier_records"
        ).fetchone()[0]

    assert len(application.bot.messages) == 1
    assert "Realtime estimate before close" in application.bot.messages[0]["text"]
    assert event["alert_key"] == "1:drawdown_plan:pre_alert:2024-01-03"
    assert event["notification_status"] == "sent"
    assert cycle_count == 1
    assert tier_count == 0
    assert provider.realtime_calls == ["510300"]
    assert provider.price_bases == [PriceBasis.QFQ]


def test_before_close_catches_up_missed_confirmed_plan_tiers(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _plan_history([100]),
            expected_date=date(2024, 1, 1),
        )
    application = FakeApplication()
    provider = FakeProvider(
        _plan_history([100, 80]),
        realtime_quote=RealtimeQuote(
            symbol="510300",
            price=90,
            previous_close=80,
            volume=100,
            amount=1000,
            source="eastmoney",
            fetched_at=datetime(2024, 1, 3, 6, 50, tzinfo=ZoneInfo("UTC")),
        ),
    )

    asyncio.run(
        scheduler.run_scheduled_before_close_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=FakeMarketCalendar(is_trading_day=True),
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 3),
        )
    )

    with open_connection(sqlite_path) as connection:
        cycle = connection.execute(
            "SELECT last_evaluated_date FROM drawdown_cycles WHERE end_date IS NULL"
        ).fetchone()
        tiers = connection.execute(
            "SELECT tier_key FROM drawdown_tier_records ORDER BY drawdown"
        ).fetchall()
        events = connection.execute(
            "SELECT alert_key, message FROM alert_events"
        ).fetchall()

    assert cycle["last_evaluated_date"] == "2024-01-02"
    assert [row["tier_key"] for row in tiers] == ["0.15", "0.2"]
    assert len(application.bot.messages) == 1
    message = application.bot.messages[0]
    assert "Buy-plan reminder — A500" in message["text"]
    assert "/mark_added" not in message["text"]
    assert "/sync_position" in message["text"]
    assert "reply_markup" not in message
    assert all("pre_alert" not in row["alert_key"] for row in events)
    assert all("/mark_added" not in row["message"] for row in events)
    assert any("/sync_position" in row["message"] for row in events)


def test_failed_before_close_prealert_is_not_retried_after_expiry(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    provider = FakeProvider(
        _plan_history([100]),
        realtime_quote=RealtimeQuote(
            symbol="510300",
            price=84,
            previous_close=100,
            volume=100,
            amount=1000,
            source="eastmoney",
            fetched_at=datetime(2024, 1, 2, 6, 50, tzinfo=ZoneInfo("UTC")),
        ),
    )
    asyncio.run(
        scheduler.run_scheduled_before_close_check(
            application=SimpleNamespace(bot=FakeFailingBot()),
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=FakeMarketCalendar(is_trading_day=True),
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )
    application = FakeApplication()

    retried = asyncio.run(
        scheduler.retry_pending_drawdown_plan_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            action_date=date(2024, 1, 3),
        )
    )

    assert retried == 0
    assert application.bot.messages == []


def test_retry_expired_close_plan_alert_strips_same_day_command(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _plan_history([100, 80]),
            expected_date=date(2024, 1, 2),
        )
        assert result.notification is not None

    application = FakeApplication()
    retried = asyncio.run(
        scheduler.retry_pending_drawdown_plan_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            action_date=date(2024, 1, 3),
        )
    )

    assert retried == 1
    assert len(application.bot.messages) == 1
    message = application.bot.messages[0]
    assert "/mark_added" not in message["text"]
    assert "/sync_position" in message["text"]
    assert "reply_markup" not in message


def test_before_close_plan_uses_sina_when_eastmoney_quote_fails_validation(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    fetched_at = datetime(2024, 1, 2, 6, 50, tzinfo=ZoneInfo("UTC"))
    provider = FakeProvider(
        _plan_history([100]),
        realtime_quote=RealtimeQuote("510300", 84, 100, 0, 0, "eastmoney", fetched_at),
        sina_quote=RealtimeQuote(
            "510300", 84, 100, 100, 1000, "sina_fallback", fetched_at
        ),
    )
    application = FakeApplication()

    asyncio.run(
        scheduler.run_scheduled_before_close_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=FakeMarketCalendar(is_trading_day=True),
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )

    assert provider.realtime_calls == ["510300"]
    assert provider.sina_calls == ["510300"]
    assert "Quote source: sina_fallback" in application.bot.messages[0]["text"]


def test_scheduled_market_check_evaluates_profit_rules(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_profit_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(
        _history(["2024-01-01"], [100.0]),
        latest={"date": "2024-01-02", "close": 2.4, "source": "test"},
    )
    market_calendar = FakeMarketCalendar(is_trading_day=True)

    asyncio.run(
        scheduler.run_scheduled_market_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=market_calendar,
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )

    with open_connection(sqlite_path) as connection:
        event_rows = connection.execute(
            """
            SELECT alert_key, notification_status
            FROM alert_events
            ORDER BY id
            """
        ).fetchall()

    assert [call.asset_type for call in provider.latest_calls] == [AssetType.CN_ETF]
    assert [row["alert_key"] for row in event_rows] == [
        "159915:profit:cost:1.85:threshold:0.25"
    ]
    assert [row["notification_status"] for row in event_rows] == ["sent"]
    assert application.bot.messages == [
        {
            "chat_id": 123,
            "text": EXPECTED_PROFIT_MESSAGE,
        }
    ]


def test_scheduled_market_check_confirms_drawdown_plan_once(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_plan_history([100, 80]))
    market_calendar = FakeMarketCalendar(is_trading_day=True)

    for _ in range(2):
        asyncio.run(
            scheduler.run_scheduled_market_check(
                application=application,
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
                market_data_provider=provider,
                market_calendar=market_calendar,
                timezone="Asia/Shanghai",
                run_date=date(2024, 1, 2),
            )
        )

    with open_connection(sqlite_path) as connection:
        event = connection.execute(
            "SELECT notification_status FROM alert_events"
        ).fetchone()
        tiers = connection.execute(
            "SELECT tier_key FROM drawdown_tier_records ORDER BY drawdown"
        ).fetchall()

    assert len(application.bot.messages) == 1
    assert "Buy-plan reminder — A500" in application.bot.messages[0]["text"]
    assert "Data date: 2024-01-02" in application.bot.messages[0]["text"]
    markup = application.bot.messages[0]["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "drawdown_add:1:1:all"
    assert [row["tier_key"] for row in tiers] == ["0.15", "0.2"]
    assert event["notification_status"] == "sent"
    assert provider.price_bases == [PriceBasis.QFQ]


def test_scheduled_fund_nav_process_applies_pending_add_once(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
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
        result = evaluate_drawdown_plan_rule(
            connection,
            rule,
            _plan_history([100, 80]),
            expected_date=date(2024, 1, 2),
        )
        assert result.notification is not None
        estimate_id, recorded = record_manual_addition(
            connection,
            rule_id=int(rule["id"]),
            cycle_id=result.cycle_id,
            source_alert_event_id=result.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000, "0.15"),),
            action_at=datetime(2024, 1, 2, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            create_estimate=True,
            cutoff_time="15:00",
            cutoff_choice="before",
            effective_date="2024-01-02",
        )
        connection.execute(
            "UPDATE alert_events SET notification_status = 'sent' WHERE id = ?",
            (result.notification.event_id,),
        )
        connection.commit()

    provider = FakeProvider(
        _plan_history([100, 80]),
        fund_nav=FundNav(
            "000001",
            date(2024, 1, 2),
            1.25,
            "akshare_eastmoney",
        ),
    )
    application = FakeApplication()
    kwargs = {
        "application": application,
        "sqlite_path": sqlite_path,
        "allowed_user_ids": {123},
        "market_data_provider": provider,
        "market_calendar": FakeMarketCalendar(is_trading_day=True),
        "timezone": "Asia/Shanghai",
        "run_date": date(2024, 1, 3),
    }

    asyncio.run(scheduler.run_scheduled_fund_nav_process(**kwargs))
    asyncio.run(scheduler.run_scheduled_fund_nav_process(**kwargs))

    with open_connection(sqlite_path) as connection:
        position = get_position_snapshot(connection, "000001")
        occurrence = connection.execute(
            "SELECT status FROM manual_add_estimates WHERE id = ?",
            (estimate_id,),
        ).fetchone()

    assert recorded == ("0.15",)
    assert occurrence["status"] == "applied"
    assert position["is_estimated"] == 1
    assert provider.nav_calls == [("000001", date(2024, 1, 2))]
    assert len(application.bot.messages) == 1
    assert "Manual addition estimate updated" in application.bot.messages[0]["text"]


def test_scheduled_market_check_notifies_once_when_plan_close_is_missing(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_plan_history([100]))
    market_calendar = FakeMarketCalendar(is_trading_day=True)

    for _ in range(2):
        asyncio.run(
            scheduler.run_scheduled_market_check(
                application=application,
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
                market_data_provider=provider,
                market_calendar=market_calendar,
                timezone="Asia/Shanghai",
                run_date=date(2024, 1, 2),
            )
        )

    with open_connection(sqlite_path) as connection:
        event = connection.execute(
            "SELECT alert_key, notification_status FROM alert_events"
        ).fetchone()

    assert event["alert_key"] == "data_unavailable:after_close:2024-01-02"
    assert event["notification_status"] == "sent"
    assert len(application.bot.messages) == 1
    assert "Drawdown plan data unavailable" in application.bot.messages[0]["text"]
    assert "No tier decision was made" in application.bot.messages[0]["text"]


def test_plan_close_does_not_mutate_state_without_confirmed_calendar(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_plan_history([100, 80]))
    market_calendar = FakeMarketCalendar(
        is_trading_day=True,
        confirmed_error=MarketCalendarUnavailableError("calendar unavailable"),
    )

    asyncio.run(
        scheduler.run_scheduled_market_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=market_calendar,
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )

    with open_connection(sqlite_path) as connection:
        cycles = connection.execute("SELECT COUNT(*) FROM drawdown_cycles").fetchone()[
            0
        ]
        tiers = connection.execute(
            "SELECT COUNT(*) FROM drawdown_tier_records"
        ).fetchone()[0]

    assert provider.calls == []
    assert cycles == 0
    assert tiers == 0
    assert market_calendar.confirmed_dates == [date(2024, 1, 2)]
    assert len(application.bot.messages) == 1
    assert "calendar unavailable" in application.bot.messages[0]["text"]


def test_failed_plan_notification_retries_even_when_market_is_closed(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_plan(sqlite_path)
    provider = FakeProvider(_plan_history([100, 80]))

    asyncio.run(
        scheduler.run_scheduled_market_check(
            application=SimpleNamespace(bot=FakeFailingBot()),
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=FakeMarketCalendar(is_trading_day=True),
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 2),
        )
    )
    success_application = FakeApplication()
    asyncio.run(
        scheduler.run_scheduled_market_check(
            application=success_application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
            market_calendar=FakeMarketCalendar(is_trading_day=False),
            timezone="Asia/Shanghai",
            run_date=date(2024, 1, 3),
        )
    )

    with open_connection(sqlite_path) as connection:
        status = connection.execute(
            "SELECT notification_status FROM alert_events"
        ).fetchone()["notification_status"]

    assert status == "sent"
    assert len(success_application.bot.messages) == 1
    assert "Buy-plan reminder — A500" in success_application.bot.messages[0]["text"]
    assert provider.price_bases == [PriceBasis.QFQ]


def test_scheduled_dca_check_prevents_duplicate_alerts_by_alert_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_dca_rule(sqlite_path)
    application = FakeApplication()
    webhook_calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> object:
        webhook_calls.append({"url": url, **kwargs})
        return FakeResponse(status_code=200)

    monkeypatch.setattr(
        "fund_alert_bot.notifications.webhook.requests.post",
        fake_post,
    )

    for _ in range(2):
        asyncio.run(
            scheduler.run_scheduled_dca_check(
                application=application,
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
                timezone="Asia/Shanghai",
                run_date=date(2024, 1, 4),
                notification_settings=NotificationSettings(
                    webhook_enabled=True,
                    webhook_url="https://hooks.example.test/secret",
                ),
            )
        )

    with open_connection(sqlite_path) as connection:
        event_rows = connection.execute(
            """
            SELECT alert_key
            FROM alert_events
            ORDER BY id
            """
        ).fetchall()

    expected_message = EXPECTED_DCA_MESSAGE
    assert [row["alert_key"] for row in event_rows] == ["dca:1:2024-01-04"]
    assert application.bot.messages == [{"chat_id": 123, "text": expected_message}]
    assert webhook_calls == [
        {
            "url": "https://hooks.example.test/secret",
            "json": {
                "title": "DCA reminder",
                "body": expected_message,
            },
            "timeout": 10,
        }
    ]


class FakeProvider:
    def __init__(
        self,
        history: pd.DataFrame,
        *,
        latest: dict[str, object] | None = None,
        realtime_quote: RealtimeQuote | None = None,
        sina_quote: RealtimeQuote | None = None,
        fund_nav: FundNav | None = None,
    ) -> None:
        self.history = history
        self.latest = latest
        self.realtime_quote = realtime_quote
        self.sina_quote = sina_quote
        self.fund_nav = fund_nav
        self.calls: list[tuple[Instrument, object, object]] = []
        self.latest_calls: list[Instrument] = []
        self.price_bases: list[PriceBasis] = []
        self.realtime_calls: list[str] = []
        self.sina_calls: list[str] = []
        self.nav_calls: list[tuple[str, object | None]] = []

    def get_history(
        self,
        instrument: Instrument,
        start_date: object,
        end_date: object,
        *,
        price_basis: PriceBasis = PriceBasis.UNADJUSTED,
    ) -> pd.DataFrame:
        self.calls.append((instrument, start_date, end_date))
        self.price_bases.append(price_basis)
        return self.history

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        self.latest_calls.append(instrument)
        return self.latest

    def get_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        self.realtime_calls.append(instrument.symbol)
        if self.realtime_quote is None:
            raise RuntimeError("No realtime quote configured.")
        return self.realtime_quote

    def get_sina_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        self.sina_calls.append(instrument.symbol)
        if self.sina_quote is None:
            raise RuntimeError("No Sina quote configured.")
        return self.sina_quote

    def get_fund_nav(
        self,
        instrument: Instrument,
        nav_date: object | None = None,
    ) -> FundNav:
        self.nav_calls.append((instrument.symbol, nav_date))
        if self.fund_nav is None:
            raise RuntimeError("No fund NAV configured.")
        return self.fund_nav


class FakeMarketCalendar:
    def __init__(
        self,
        *,
        is_trading_day: bool,
        confirmed_error: Exception | None = None,
    ) -> None:
        self._is_trading_day = is_trading_day
        self._confirmed_error = confirmed_error
        self.checked_dates: list[date] = []
        self.confirmed_dates: list[date] = []

    def is_trading_day(self, check_date: date) -> bool:
        self.checked_dates.append(check_date)
        return self._is_trading_day

    def confirmed_status(self, check_date: date) -> bool:
        self.confirmed_dates.append(check_date)
        if self._confirmed_error is not None:
            raise self._confirmed_error
        return self._is_trading_day


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}

    def add_job(self, func: object, **kwargs: object) -> None:
        job_id = str(kwargs["id"])
        self.jobs[job_id] = {"func": func, **kwargs}


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: object | None = None,
    ) -> None:
        message = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            message["reply_markup"] = reply_markup
        self.messages.append(message)


class FakeFailingBot:
    async def send_message(self, *, chat_id: int, text: str) -> None:
        del chat_id, text
        raise RuntimeError("telegram unavailable")


class FakeApplication(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(bot=FakeBot())


class FakeResponse:
    def __init__(self, *, status_code: int) -> None:
        self.status_code = status_code


def _add_drawdown_rule(sqlite_path: Path) -> None:
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        add_rule(
            connection,
            type=commands.DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="ChiNext Index",
            asset_type=AssetType.CN_INDEX.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.10],
                "price_field": "close",
            },
        )


def _add_drawdown_plan(sqlite_path: Path) -> None:
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="A500",
            asset_type="cn_etf",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "tiers": [
                    {"drawdown": 0.15, "amount": 5000},
                    {"drawdown": 0.20, "amount": 10000},
                ],
                "sma_window": 250,
                "sma_slope_window": 20,
            },
        )


def _plan_history(closes: list[float]) -> pd.DataFrame:
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


def _add_dca_rule(sqlite_path: Path) -> None:
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        add_rule(
            connection,
            type=commands.DCA_RULE_TYPE,
            symbol="创业板",
            name="创业板",
            asset_type="dca",
            params={
                "weekday": "THU",
                "amount": 1000,
            },
        )


def _add_profit_rule(sqlite_path: Path) -> None:
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        add_rule(
            connection,
            type=commands.PROFIT_RULE_TYPE,
            symbol="159915",
            name="ChiNext ETF",
            asset_type=AssetType.CN_ETF.value,
            params={
                "cost": 1.85,
                "thresholds": [0.25],
            },
        )


def _history(dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(closes),
            "amount": [10000] * len(closes),
            "source": ["test"] * len(closes),
        }
    )
