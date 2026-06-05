from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from fund_alert_bot.market_data.calendar import CNMarketCalendar


class FakeAkshareCalendar:
    def __init__(
        self,
        raw_data: Any | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.raw_data = raw_data
        self.error = error
        self.calls = 0

    def tool_trade_date_hist_sina(self) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.raw_data is None:
            return pd.DataFrame()
        return self.raw_data


class FakeAkshareWithoutCalendar:
    pass


def test_cn_market_calendar_uses_akshare_trade_dates_and_caches() -> None:
    fake_ak = FakeAkshareCalendar(
        pd.DataFrame({"trade_date": ["2024-04-30", "2024-05-06"]})
    )
    calendar = CNMarketCalendar(ak_module=fake_ak)

    assert calendar.is_trading_day(date(2024, 4, 30)) is True
    assert calendar.is_trading_day(date(2024, 5, 1)) is False
    assert calendar.is_trading_day(date(2024, 5, 6)) is True
    assert fake_ak.calls == 1


def test_cn_market_calendar_accepts_chinese_date_column() -> None:
    fake_ak = FakeAkshareCalendar(
        pd.DataFrame({"\u65e5\u671f": ["2024-01-02", "2024-01-03"]})
    )
    calendar = CNMarketCalendar(ak_module=fake_ak)

    assert calendar.is_trading_day(date(2024, 1, 2)) is True
    assert calendar.is_trading_day(date(2024, 1, 1)) is False


def test_cn_market_calendar_accepts_numeric_akshare_dates() -> None:
    fake_ak = FakeAkshareCalendar(pd.DataFrame({"trade_date": [20240102]}))
    calendar = CNMarketCalendar(ak_module=fake_ak)

    assert calendar.is_trading_day(date(2024, 1, 2)) is True


def test_cn_market_calendar_falls_back_to_weekday_when_akshare_fails() -> None:
    fake_ak = FakeAkshareCalendar(error=RuntimeError("AKShare unavailable"))
    calendar = CNMarketCalendar(ak_module=fake_ak)

    assert calendar.is_trading_day(date(2024, 5, 1)) is True
    assert calendar.is_trading_day(date(2024, 5, 4)) is False


def test_cn_market_calendar_falls_back_when_akshare_tool_is_missing() -> None:
    calendar = CNMarketCalendar(ak_module=FakeAkshareWithoutCalendar())

    assert calendar.is_trading_day(date(2024, 5, 1)) is True
    assert calendar.is_trading_day(date(2024, 5, 4)) is False


def test_cn_market_calendar_falls_back_when_calendar_payload_is_unusable() -> None:
    fake_ak = FakeAkshareCalendar(pd.DataFrame({"not_a_date": ["2024-05-01"]}))
    calendar = CNMarketCalendar(ak_module=fake_ak)

    assert calendar.is_trading_day(date(2024, 5, 1)) is True


def test_cn_market_calendar_handles_dataframe_like_payloads() -> None:
    fake_ak = FakeAkshareCalendar([{"trade_date": "2024-01-02"}])
    calendar = CNMarketCalendar(ak_module=fake_ak)

    assert calendar.is_trading_day(date(2024, 1, 2)) is True
