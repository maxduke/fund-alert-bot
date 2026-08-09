"""CN market trading calendar helpers."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol

import pandas as pd

from fund_alert_bot.market_data.exceptions import MarketCalendarUnavailableError

LOGGER = logging.getLogger(__name__)

AKSHARE_TRADE_DATE_COLUMNS = ("trade_date", "\u65e5\u671f")


class MarketCalendar(Protocol):
    """Calendar interface used by scheduled market checks."""

    def is_trading_day(self, check_date: date) -> bool:
        """Return whether the market is expected to trade on check_date."""
        ...

    def confirmed_status(self, check_date: date) -> bool:
        """Return provider-confirmed status or fail when coverage is unavailable."""
        ...


class CNMarketCalendar:
    """CN trading calendar backed by AKShare with weekday fallback."""

    def __init__(self, *, ak_module: Any | None = None) -> None:
        self._ak_module = ak_module
        self._trade_days: set[date] | None = None

    def is_trading_day(self, check_date: date) -> bool:
        """Return True when check_date is a CN trading day."""

        was_cached = self._trade_days is not None
        trade_days = self._load_trade_days()
        if was_cached and trade_days is not None and check_date > max(trade_days):
            trade_days = self._load_trade_days(refresh=True)
        if trade_days is None or check_date > max(trade_days):
            return is_cn_market_weekday(check_date)
        return check_date in trade_days

    def confirmed_status(self, check_date: date) -> bool:
        """Return status only when the loaded calendar covers check_date."""

        was_cached = self._trade_days is not None
        trade_days = self._load_trade_days()
        if trade_days is None:
            raise MarketCalendarUnavailableError("CN trade calendar is unavailable.")
        if was_cached and not _covers(trade_days, check_date):
            trade_days = self._load_trade_days(refresh=True)
        if trade_days is None or not _covers(trade_days, check_date):
            raise MarketCalendarUnavailableError(
                f"CN trade calendar does not cover {check_date.isoformat()}."
            )
        return check_date in trade_days

    def _load_trade_days(self, *, refresh: bool = False) -> set[date] | None:
        if self._trade_days is not None and not refresh:
            return self._trade_days

        try:
            raw_data = self._akshare.tool_trade_date_hist_sina()
            trade_days = _extract_trade_days(raw_data)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Failed to load CN trade calendar from AKShare; "
                "falling back to weekday logic: %s",
                exc,
            )
            return None

        if trade_days is None:
            LOGGER.warning(
                "AKShare CN trade calendar was empty or missing a date column; "
                "falling back to weekday logic."
            )
            return None

        self._trade_days = trade_days
        return self._trade_days

    @property
    def _akshare(self) -> Any:
        if self._ak_module is None:
            import akshare as ak

            self._ak_module = ak
        return self._ak_module


def is_cn_market_weekday(check_date: date) -> bool:
    """Return True for Monday-Friday fallback scheduling."""

    return check_date.weekday() < 5


def _covers(trade_days: set[date], check_date: date) -> bool:
    return min(trade_days) <= check_date <= max(trade_days)


def _extract_trade_days(raw_data: Any) -> set[date] | None:
    if raw_data is None:
        return None

    frame = pd.DataFrame(raw_data)
    if frame.empty:
        return None

    date_column = _find_trade_date_column(frame)
    if date_column is None:
        return None

    raw_dates = frame[date_column]
    if pd.api.types.is_numeric_dtype(raw_dates):
        parsed_dates = pd.to_datetime(
            raw_dates.astype(str),
            format="%Y%m%d",
            errors="coerce",
        )
    else:
        parsed_dates = pd.to_datetime(raw_dates, errors="coerce")

    trade_days = set(parsed_dates.dropna().dt.date.tolist())
    if not trade_days:
        return None
    return trade_days


def _find_trade_date_column(frame: pd.DataFrame) -> str | None:
    for column in AKSHARE_TRADE_DATE_COLUMNS:
        if column in frame.columns:
            return column
    return None
