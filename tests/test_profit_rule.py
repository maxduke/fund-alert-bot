from __future__ import annotations

import pytest

from fund_alert_bot.checks import PROFIT_RULE_TYPE, evaluate_profit_rules
from fund_alert_bot.db import add_rule, connect, init_db
from fund_alert_bot.market_data import AssetType, Instrument
from fund_alert_bot.rules.profit import (
    build_profit_alert_key,
    build_profit_alerts,
    calculate_profit_rate,
)


def test_profit_calculation() -> None:
    profit_rate = calculate_profit_rate(current_price=1.5425, cost=1.234)

    assert profit_rate == pytest.approx(0.25)


def test_profit_rule_triggers_crossed_thresholds() -> None:
    latest = {"date": "2024-01-02", "close": 2.4, "source": "test"}
    rule = _rule(cost=1.85, thresholds=[0.25, 0.40])

    alerts = build_profit_alerts(rule, latest, _never_seen)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["alert_key"] == "159915:profit:cost:1.85:threshold:0.25"
    assert alert["title"] == "💵 Profit-taking reminder"
    assert alert["payload"] == {
        "symbol": "159915",
        "name": "ChiNext ETF",
        "asset_type": "cn_etf",
        "cost": 1.85,
        "latest_price": 2.4,
        "latest_date": "2024-01-02",
        "profit_rate": pytest.approx(0.2972972973),
        "threshold": 0.25,
        "source": "test",
    }
    assert "• Symbol: 159915" in str(alert["message"])
    assert "• Latest price: 2.4" in str(alert["message"])
    assert "Profit rate: 29.7%" in str(alert["message"])
    assert "Triggered threshold: 25.0%" in str(alert["message"])
    assert "not automatic trading" in str(alert["message"])


def test_open_fund_profit_rule_uses_latest_nav_close() -> None:
    latest = {"date": "2024-01-02", "close": 1.25, "source": "test"}
    rule = _rule(
        symbol="110026",
        name="Example Open Fund",
        asset_type=AssetType.CN_OPEN_FUND.value,
        cost=1.0,
        thresholds=[0.25],
    )

    alerts = build_profit_alerts(rule, latest, _never_seen)

    assert len(alerts) == 1
    assert alerts[0]["payload"]["latest_price"] == 1.25
    assert "Latest NAV: 1.25" in str(alerts[0]["message"])


def test_profit_rule_skips_existing_threshold_for_cost_basis() -> None:
    latest = {"date": "2024-01-02", "close": 2.4, "source": "test"}
    rule = _rule(cost=1.85, thresholds=[0.25])
    existing_keys = {"159915:profit:cost:1.85:threshold:0.25"}

    alerts = build_profit_alerts(rule, latest, existing_keys.__contains__)

    assert alerts == []


def test_profit_alert_key_includes_cost_basis_and_threshold() -> None:
    assert build_profit_alert_key(symbol="300750", cost=180, threshold=0.25) == (
        "300750:profit:cost:180:threshold:0.25"
    )


def test_evaluate_profit_rules_reports_unavailable_latest_data() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="159915",
            name="ChiNext ETF",
            asset_type=AssetType.CN_ETF.value,
            params={"cost": 1.85, "thresholds": [0.25]},
        )
        result = evaluate_profit_rules(connection, FakeLatestProvider(None))
    finally:
        connection.close()

    assert result.checked_rules == 1
    assert result.notifications == []
    assert result.no_data_skips[0].message == (
        "Latest price is unavailable for 159915."
    )


def test_evaluate_profit_rules_sends_threshold_once_per_cost_basis() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="159915",
            name="ChiNext ETF",
            asset_type=AssetType.CN_ETF.value,
            params={"cost": 1.85, "thresholds": [0.25]},
        )
        provider = FakeLatestProvider(
            {"date": "2024-01-02", "close": 2.4, "source": "test"}
        )

        first_result = evaluate_profit_rules(connection, provider)
        second_result = evaluate_profit_rules(connection, provider)
        event_rows = connection.execute(
            """
            SELECT alert_key
            FROM alert_events
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()

    assert len(first_result.notifications) == 1
    assert len(second_result.notifications) == 0
    assert [row["alert_key"] for row in event_rows] == [
        "159915:profit:cost:1.85:threshold:0.25"
    ]


class FakeLatestProvider:
    def __init__(self, latest: dict[str, object] | None) -> None:
        self.latest = latest
        self.latest_calls: list[Instrument] = []

    def get_history(
        self,
        instrument: Instrument,
        start_date: object,
        end_date: object,
    ) -> object:
        raise AssertionError("profit reminders should use get_latest")

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        self.latest_calls.append(instrument)
        return self.latest


def _rule(
    *,
    cost: float,
    thresholds: list[float],
    symbol: str = "159915",
    name: str = "ChiNext ETF",
    asset_type: str = "cn_etf",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": name,
        "asset_type": asset_type,
        "params": {
            "cost": cost,
            "thresholds": thresholds,
        },
    }


def _never_seen(_alert_key: str) -> bool:
    return False
