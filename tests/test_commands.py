from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from fund_alert_bot.commands import (
    DCA_RULE_TYPE,
    DRAW_DOWN_RULE_TYPE,
    TEST_NOTIFICATION_MESSAGE,
    CommandParseError,
    build_command_handlers,
    dca_params,
    drawdown_params,
    evaluate_drawdown_rules,
    format_rules_list,
    parse_add_dca_args,
    parse_add_drawdown_args,
    parse_thresholds,
)
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import add_rule, connect, init_db, list_rules, open_connection
from fund_alert_bot.market_data import AssetType, Instrument
from fund_alert_bot.rules.dca import weekday_for_date


def test_parse_valid_drawdown_command() -> None:
    command = parse_add_drawdown_args(
        ["cn_index", "399006", "创业板指", "365", "10,15,20"]
    )

    assert command.asset_type is AssetType.CN_INDEX
    assert command.symbol == "399006"
    assert command.name == "创业板指"
    assert command.lookback_days == 365
    assert drawdown_params(command) == {
        "lookback_days": 365,
        "thresholds": [0.10, 0.15, 0.20],
        "price_field": "close",
    }


def test_parse_valid_dca_command_with_chinese_weekday() -> None:
    command = parse_add_dca_args(["创业板", "周四", "1000"])

    assert command.name == "创业板"
    assert command.weekday == "THU"
    assert command.amount == 1000
    assert dca_params(command) == {"weekday": "THU", "amount": 1000}


def test_parse_valid_dca_command_with_english_weekday() -> None:
    command = parse_add_dca_args(["创业板", "Thursday", "1000"])

    assert command.name == "创业板"
    assert command.weekday == "THU"
    assert command.amount == 1000


def test_reject_invalid_asset_type() -> None:
    with pytest.raises(CommandParseError, match="Invalid asset_type"):
        parse_add_drawdown_args(["crypto", "BTC", "Bitcoin", "365", "10"])


def test_parse_thresholds_correctly() -> None:
    assert parse_thresholds("10,15,20") == [0.10, 0.15, 0.20]


def test_check_prevents_duplicate_alert_notifications() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指",
            asset_type=AssetType.CN_INDEX.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.10],
                "price_field": "close",
            },
        )
        provider = FakeProvider(_history(["2024-01-01", "2024-01-02"], [100.0, 90.0]))

        first_result = evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 2),
        )
        second_result = evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 2),
        )
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    finally:
        connection.close()

    assert len(first_result.notifications) == 1
    assert len(second_result.notifications) == 0
    assert event_count == 1
    assert [call[0].asset_type for call in provider.calls] == [
        AssetType.CN_INDEX,
        AssetType.CN_INDEX,
    ]


def test_list_shows_asset_type() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="110026",
            name="易方达创业板ETF联接A",
            asset_type=AssetType.CN_OPEN_FUND.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.10, 0.15, 0.20],
                "price_field": "close",
            },
        )

        response = format_rules_list(list_rules(connection))
    finally:
        connection.close()

    assert "type=drawdown_from_high" in response
    assert "asset_type=cn_open_fund" in response
    assert "symbol=110026" in response


def test_list_shows_dca_rule() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DCA_RULE_TYPE,
            symbol="创业板",
            name="创业板",
            asset_type="dca",
            params={"weekday": "THU", "amount": 1000},
        )

        response = format_rules_list(list_rules(connection))
    finally:
        connection.close()

    assert "type=dca_reminder" in response
    assert "name=创业板" in response
    assert 'params={"amount":1000,"weekday":"THU"}' in response


def test_add_dca_command_persists_rule(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    handlers = build_command_handlers({123}, sqlite_path=sqlite_path)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=["创业板", "周四", "1000"])

    asyncio.run(_handler_by_command(handlers, "add_dca").callback(update, context))

    with open_connection(sqlite_path) as connection:
        rows = list_rules(connection)

    assert len(rows) == 1
    assert rows[0]["type"] == DCA_RULE_TYPE
    assert rows[0]["symbol"] == "创业板"
    assert rows[0]["name"] == "创业板"
    assert rows[0]["asset_type"] == "dca"
    assert message.replies == [
        "Added DCA rule id=1 name=创业板 weekday=THU amount=1000"
    ]


def test_check_sends_due_dca_without_market_data_fetch(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type=DCA_RULE_TYPE,
            symbol="创业板",
            name="创业板",
            asset_type="dca",
            params={"weekday": weekday_for_date(date.today()), "amount": 1000},
        )

    provider = FakeProvider(_history(["2024-01-01"], [100.0]))
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[])

    asyncio.run(_handler_by_command(handlers, "check").callback(update, context))

    assert provider.calls == []
    assert context.bot.messages == [
        {
            "chat_id": 456,
            "text": (
                "今天是 创业板 定投日，计划定投 1000 元。\n"
                "提醒：这是纪律提醒，不会自动交易。"
            ),
        }
    ]
    assert "Checked 1 dca_reminder rule(s)." in message.replies[0]


def test_test_notify_sends_to_enabled_channels(monkeypatch) -> None:
    webhook_calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> object:
        webhook_calls.append({"url": url, **kwargs})
        return FakeResponse(status_code=200)

    monkeypatch.setattr(
        "fund_alert_bot.notifications.webhook.requests.post",
        fake_post,
    )
    handlers = build_command_handlers(
        {123},
        notification_settings=NotificationSettings(
            webhook_enabled=True,
            webhook_url="https://hooks.example.test/secret",
        ),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[])

    asyncio.run(handlers[-1].callback(update, context))

    assert context.bot.messages == [
        {"chat_id": 456, "text": TEST_NOTIFICATION_MESSAGE}
    ]
    assert webhook_calls == [
        {
            "url": "https://hooks.example.test/secret",
            "json": {
                "title": "fund-alert-bot test",
                "body": TEST_NOTIFICATION_MESSAGE,
            },
            "timeout": 10,
        }
    ]
    assert message.replies == ["Sent test notification to 2 channel(s)."]


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


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})


class FakeResponse:
    def __init__(self, *, status_code: int) -> None:
        self.status_code = status_code


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


def _handler_by_command(handlers: list[object], command: str) -> object:
    for handler in handlers:
        if command in getattr(handler, "commands", ()):
            return handler
    raise AssertionError(f"handler not found: {command}")
