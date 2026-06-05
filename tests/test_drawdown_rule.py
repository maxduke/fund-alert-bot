from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from fund_alert_bot.rules.drawdown import (
    build_drawdown_alerts,
    calculate_drawdown_from_high,
)


def test_drawdown_rule_does_not_trigger_below_threshold() -> None:
    df = _history(["2024-01-01", "2024-01-02"], [100.0, 95.0])
    rule = _rule(thresholds=[0.10])

    alerts = build_drawdown_alerts(rule, df, _never_seen)

    assert alerts == []


def test_drawdown_rule_triggers_ten_percent_threshold() -> None:
    df = _history(["2024-01-01", "2024-01-02"], [100.0, 90.0])
    rule = _rule(thresholds=[0.10])

    alerts = build_drawdown_alerts(rule, df, _never_seen)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["alert_key"] == "510300:drawdown:365:peak:2024-01-01:threshold:0.1"
    assert alert["payload"] == {
        "symbol": "510300",
        "name": "CSI 300 ETF",
        "asset_type": "cn_etf",
        "latest_date": "2024-01-02",
        "latest_close": 90.0,
        "peak_date": "2024-01-01",
        "peak_close": 100.0,
        "drawdown": pytest.approx(0.10),
        "threshold": 0.10,
        "source": "test",
    }


def test_drawdown_rule_triggers_fifteen_percent_threshold() -> None:
    df = _history(["2024-01-01", "2024-01-02"], [100.0, 85.0])
    rule = _rule(thresholds=[0.15])

    alerts = build_drawdown_alerts(rule, df, _never_seen)

    assert len(alerts) == 1
    assert alerts[0]["payload"]["drawdown"] == pytest.approx(0.15)
    assert alerts[0]["payload"]["threshold"] == 0.15


def test_drawdown_rule_triggers_all_crossed_thresholds() -> None:
    df = _history(["2024-01-01", "2024-01-02"], [100.0, 80.0])
    rule = _rule(thresholds=[0.10, 0.15, 0.20])

    alerts = build_drawdown_alerts(rule, df, _never_seen)

    assert [alert["payload"]["threshold"] for alert in alerts] == [0.10, 0.15, 0.20]
    assert [alert["alert_key"] for alert in alerts] == [
        "510300:drawdown:365:peak:2024-01-01:threshold:0.1",
        "510300:drawdown:365:peak:2024-01-01:threshold:0.15",
        "510300:drawdown:365:peak:2024-01-01:threshold:0.2",
    ]


def test_drawdown_rule_skips_existing_peak_and_threshold_alert() -> None:
    df = _history(["2024-01-01", "2024-01-02"], [100.0, 90.0])
    rule = _rule(thresholds=[0.10])
    existing_keys = {"510300:drawdown:365:peak:2024-01-01:threshold:0.1"}

    alerts = build_drawdown_alerts(rule, df, existing_keys.__contains__)

    assert alerts == []


def test_drawdown_rule_new_peak_creates_different_alert_key() -> None:
    df = _history(
        ["2024-01-01", "2024-01-02", "2024-01-03"],
        [100.0, 110.0, 99.0],
    )
    rule = _rule(thresholds=[0.10])
    existing_keys = {"510300:drawdown:365:peak:2024-01-01:threshold:0.1"}

    alerts = build_drawdown_alerts(rule, df, existing_keys.__contains__)

    assert len(alerts) == 1
    assert alerts[0]["alert_key"] == "510300:drawdown:365:peak:2024-01-02:threshold:0.1"


def test_drawdown_rule_works_with_open_fund_data() -> None:
    df = _history(
        ["2024-01-01", "2024-01-02"],
        [1.0, 0.85],
        asset_empty_price_columns=True,
    )
    rule = _rule(
        thresholds=[0.15],
        symbol="000001",
        name="Example Open Fund",
        asset_type="cn_open_fund",
    )

    alerts = build_drawdown_alerts(rule, df, _never_seen)

    assert len(alerts) == 1
    assert alerts[0]["payload"]["symbol"] == "000001"
    assert alerts[0]["payload"]["asset_type"] == "cn_open_fund"
    assert alerts[0]["payload"]["drawdown"] == pytest.approx(0.15)


def test_calculate_drawdown_uses_calendar_lookback_window() -> None:
    df = _history(
        ["2023-01-01", "2024-01-01", "2024-01-02"],
        [200.0, 100.0, 90.0],
    )

    result = calculate_drawdown_from_high(df, lookback_days=365)

    assert result["peak_date"] == "2024-01-01"
    assert result["peak_price"] == 100.0
    assert result["drawdown"] == pytest.approx(0.10)


def _history(
    dates: list[str],
    closes: list[float],
    *,
    asset_empty_price_columns: bool = False,
) -> pd.DataFrame:
    if asset_empty_price_columns:
        open_values = high_values = low_values = volume_values = amount_values = None
    else:
        open_values = closes
        high_values = closes
        low_values = closes
        volume_values = [1000] * len(closes)
        amount_values = [10000] * len(closes)

    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": open_values,
            "high": high_values,
            "low": low_values,
            "close": closes,
            "volume": volume_values,
            "amount": amount_values,
            "source": ["test"] * len(closes),
        }
    )


def _rule(
    *,
    thresholds: list[float],
    symbol: str = "510300",
    name: str = "CSI 300 ETF",
    asset_type: str = "cn_etf",
    lookback_days: int = 365,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "asset_type": asset_type,
        "params": {
            "lookback_days": lookback_days,
            "thresholds": thresholds,
            "price_field": "close",
        },
    }


def _never_seen(_alert_key: str) -> bool:
    return False
