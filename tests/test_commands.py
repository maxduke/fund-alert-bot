from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fund_alert_bot.commands import (
    DRAW_DOWN_RULE_TYPE,
    CommandParseError,
    drawdown_params,
    evaluate_drawdown_rules,
    format_rules_list,
    parse_add_drawdown_args,
    parse_thresholds,
)
from fund_alert_bot.db import add_rule, connect, init_db, list_rules
from fund_alert_bot.market_data import AssetType, Instrument


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
