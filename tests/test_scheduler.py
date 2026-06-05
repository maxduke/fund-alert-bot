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
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import add_rule, initialize_database, open_connection
from fund_alert_bot.market_data import AssetType, Instrument


def test_scheduler_time_parsing() -> None:
    parsed_time = scheduler.parse_after_close_check_time("17:10")

    assert parsed_time.hour == 17
    assert parsed_time.minute == 10


def test_dca_reminder_time_parsing() -> None:
    parsed_time = scheduler.parse_dca_reminder_time("09:30")

    assert parsed_time.hour == 9
    assert parsed_time.minute == 30


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
        {"chat_id": 123, "text": "399006 is down 10.0% from its 365-day high."}
    ]
    assert webhook_calls == [
        {
            "url": "https://hooks.example.test/secret",
            "json": {
                "title": "Drawdown reminder",
                "body": "399006 is down 10.0% from its 365-day high.",
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


def test_register_jobs_passes_calendar_only_to_after_close_job() -> None:
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

    after_close_job = fake_scheduler.jobs[scheduler.MARKET_AFTER_CLOSE_JOB_ID]
    dca_job = fake_scheduler.jobs[scheduler.DCA_MORNING_JOB_ID]

    assert after_close_job["func"] is scheduler.run_scheduled_market_check
    assert after_close_job["kwargs"]["market_calendar"] is market_calendar
    assert after_close_job["kwargs"]["market_data_provider"] is provider
    assert "market_calendar" not in dca_job["kwargs"]


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
            "text": (
                "Profit-taking reminder\n"
                "Symbol: 159915\n"
                "Name: ChiNext ETF\n"
                "Asset type: cn_etf\n"
                "Cost: 1.85\n"
                "Latest price: 2.4\n"
                "Profit rate: 29.7%\n"
                "Triggered threshold: 25.0%\n"
                "Reminder: this is not automatic trading and no orders will be placed."
            ),
        }
    ]


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

    expected_message = (
        "今天是 创业板 定投日，计划定投 1000 元。\n提醒：这是纪律提醒，不会自动交易。"
    )
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
    ) -> None:
        self.history = history
        self.latest = latest
        self.calls: list[tuple[Instrument, object, object]] = []
        self.latest_calls: list[Instrument] = []

    def get_history(
        self,
        instrument: Instrument,
        start_date: object,
        end_date: object,
    ) -> pd.DataFrame:
        self.calls.append((instrument, start_date, end_date))
        return self.history

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        self.latest_calls.append(instrument)
        return self.latest


class FakeMarketCalendar:
    def __init__(self, *, is_trading_day: bool) -> None:
        self._is_trading_day = is_trading_day
        self.checked_dates: list[date] = []

    def is_trading_day(self, check_date: date) -> bool:
        self.checked_dates.append(check_date)
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

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})


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
