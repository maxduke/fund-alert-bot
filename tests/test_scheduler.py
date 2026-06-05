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
from fund_alert_bot.db import add_rule, initialize_database, open_connection
from fund_alert_bot.market_data import AssetType, Instrument


def test_scheduler_time_parsing() -> None:
    parsed_time = scheduler.parse_after_close_check_time("17:10")

    assert parsed_time.hour == 17
    assert parsed_time.minute == 10


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


def test_check_and_scheduler_use_same_evaluator() -> None:
    assert commands.evaluate_drawdown_rules is scheduler.evaluate_drawdown_rules


def test_scheduled_check_prevents_duplicate_alerts_by_alert_key(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_history(["2024-01-01", "2024-01-02"], [100.0, 90.0]))

    for _ in range(2):
        asyncio.run(
            scheduler.run_scheduled_drawdown_check(
                application=application,
                sqlite_path=sqlite_path,
                allowed_user_ids={123},
                market_data_provider=provider,
                timezone="Asia/Shanghai",
                run_date=date(2024, 1, 2),
            )
        )

    with open_connection(sqlite_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    assert event_count == 1
    assert application.bot.messages == [
        {"chat_id": 123, "text": "399006 is down 10.0% from its 365-day high."}
    ]


def test_scheduled_check_logs_and_skips_when_no_new_data(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    _add_drawdown_rule(sqlite_path)
    application = FakeApplication()
    provider = FakeProvider(_history(["2024-01-01"], [100.0]))
    caplog.set_level(logging.INFO, logger="fund_alert_bot.scheduler")

    asyncio.run(
        scheduler.run_scheduled_drawdown_check(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            market_data_provider=provider,
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
    assert "Scheduled drawdown check started" in caplog.text
    assert "Scheduled drawdown check skipped" in caplog.text
    assert "No market data available for 2024-01-02" in caplog.text
    assert "Scheduled drawdown check ended" in caplog.text


class FakeProvider:
    def __init__(self, history: pd.DataFrame) -> None:
        self.history = history
        self.calls: list[tuple[Instrument, object, object]] = []

    def get_history(
        self,
        instrument: Instrument,
        start_date: object,
        end_date: object,
    ) -> pd.DataFrame:
        self.calls.append((instrument, start_date, end_date))
        return self.history

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        return None


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})


class FakeApplication(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(bot=FakeBot())


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
