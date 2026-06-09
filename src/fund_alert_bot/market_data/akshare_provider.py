"""AKShare-backed market data provider."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pandas as pd

from fund_alert_bot.market_data.exceptions import (
    EmptyMarketDataError,
    MarketDataFetchError,
    UnsupportedAssetTypeError,
)
from fund_alert_bot.market_data.models import AssetType, Instrument
from fund_alert_bot.market_data.normalize import NORMALIZED_COLUMNS, normalize_history
from fund_alert_bot.market_data.provider import DateLike, MarketDataProvider


class AkshareMarketDataProvider(MarketDataProvider):
    """Fetch and normalize historical market data from AKShare."""

    def __init__(
        self,
        *,
        ak_module: Any | None = None,
        retries: int = 3,
        retry_delay_seconds: float = 0.5,
        latest_lookback_days: int = 45,
        realtime_spot_ttl_seconds: float = 30.0,
        today_factory: Callable[[], date] = date.today,
    ) -> None:
        if retries < 1:
            raise ValueError("retries must be at least 1")
        if latest_lookback_days < 1:
            raise ValueError("latest_lookback_days must be at least 1")
        if (
            not math.isfinite(realtime_spot_ttl_seconds)
            or realtime_spot_ttl_seconds < 0
        ):
            raise ValueError("realtime_spot_ttl_seconds must be non-negative")

        self._ak_module = ak_module
        self._retries = retries
        self._retry_delay_seconds = retry_delay_seconds
        self._latest_lookback_days = latest_lookback_days
        self._realtime_spot_ttl_seconds = realtime_spot_ttl_seconds
        self._today_factory = today_factory
        self._realtime_spot_cache: dict[AssetType, tuple[float, pd.DataFrame]] = {}

    def get_history(
        self,
        instrument: Instrument,
        start_date: DateLike,
        end_date: DateLike,
    ) -> pd.DataFrame:
        """Return normalized daily history for an instrument."""

        asset_type = self._resolve_asset_type(instrument.asset_type)
        raw_data = self._fetch_raw_history(instrument, asset_type, start_date, end_date)
        history = normalize_history(raw_data, asset_type, source="akshare")
        history = self._filter_by_date(history, start_date, end_date)

        if history.empty:
            raise EmptyMarketDataError(
                f"No market data returned for {instrument.symbol} "
                f"between {start_date} and {end_date}."
            )
        return history[NORMALIZED_COLUMNS]

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        """Return the latest normalized row, preferring realtime spot data."""

        realtime = self._get_realtime_latest(instrument)
        if realtime is not None:
            return realtime

        end_date = self._today_factory()
        start_date = end_date - timedelta(days=self._latest_lookback_days)
        try:
            history = self.get_history(instrument, start_date, end_date)
        except EmptyMarketDataError:
            return None

        latest_row = history.iloc[-1].to_dict()
        return {
            key: None if pd.isna(value) else value for key, value in latest_row.items()
        }

    def _get_realtime_latest(
        self,
        instrument: Instrument,
    ) -> dict[str, object] | None:
        asset_type = self._resolve_asset_type(instrument.asset_type)
        if asset_type is AssetType.CN_OPEN_FUND:
            return None

        raw_data = self._fetch_raw_realtime(asset_type)
        if raw_data is None or raw_data.empty or "代码" not in raw_data.columns:
            return None

        symbol = _strip_exchange_prefix(instrument.symbol)
        matched = raw_data.loc[raw_data["代码"].astype(str) == symbol]
        if matched.empty:
            return None

        row = matched.iloc[0]
        close = _read_realtime_float(row, "最新价")
        if close is None:
            return None

        return {
            "date": pd.Timestamp(self._today_factory()),
            "open": _read_realtime_float(row, "今开"),
            "high": _read_realtime_float(row, "最高"),
            "low": _read_realtime_float(row, "最低"),
            "close": close,
            "volume": _read_realtime_float(row, "成交量"),
            "amount": _read_realtime_float(row, "成交额"),
            "source": "akshare_realtime",
        }

    def _fetch_raw_realtime(self, asset_type: AssetType) -> pd.DataFrame | None:
        cached = self._read_realtime_spot_cache(asset_type)
        if cached is not None:
            return cached

        ak_module = self._akshare
        try:
            if asset_type is AssetType.CN_INDEX:
                raw_data = self._call_with_retry(ak_module.stock_zh_index_spot_em)
            elif asset_type is AssetType.CN_ETF:
                raw_data = self._call_with_retry(ak_module.fund_etf_spot_em)
            elif asset_type is AssetType.CN_STOCK:
                raw_data = self._call_with_retry(ak_module.stock_zh_a_spot_em)
            else:
                return None
        except (AttributeError, MarketDataFetchError):
            return None

        self._write_realtime_spot_cache(asset_type, raw_data)
        return raw_data

    def _read_realtime_spot_cache(
        self,
        asset_type: AssetType,
    ) -> pd.DataFrame | None:
        if self._realtime_spot_ttl_seconds <= 0:
            return None

        cached = self._realtime_spot_cache.get(asset_type)
        if cached is None:
            return None

        cached_at, raw_data = cached
        if time.monotonic() - cached_at <= self._realtime_spot_ttl_seconds:
            return raw_data

        self._realtime_spot_cache.pop(asset_type, None)
        return None

    def _write_realtime_spot_cache(
        self,
        asset_type: AssetType,
        raw_data: pd.DataFrame,
    ) -> None:
        if self._realtime_spot_ttl_seconds <= 0:
            return
        self._realtime_spot_cache[asset_type] = (time.monotonic(), raw_data)

    def _fetch_raw_history(
        self,
        instrument: Instrument,
        asset_type: AssetType,
        start_date: DateLike,
        end_date: DateLike,
    ) -> pd.DataFrame:
        start = _format_akshare_date(start_date)
        end = _format_akshare_date(end_date)
        ak_module = self._akshare

        if asset_type is AssetType.CN_INDEX:
            return self._call_with_retry(
                ak_module.stock_zh_index_daily_em,
                symbol=_format_cn_index_symbol(instrument.symbol),
            )
        if asset_type is AssetType.CN_ETF:
            return self._fetch_cn_etf_history(instrument, start, end)
        if asset_type is AssetType.CN_STOCK:
            return self._call_with_retry(
                ak_module.stock_zh_a_hist,
                symbol=instrument.symbol,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="",
            )
        if asset_type is AssetType.CN_OPEN_FUND:
            return self._call_with_retry(
                ak_module.fund_open_fund_info_em,
                symbol=instrument.symbol,
                indicator="\u5355\u4f4d\u51c0\u503c\u8d70\u52bf",
            )

        raise UnsupportedAssetTypeError(f"Unsupported asset type: {asset_type!r}")

    def _fetch_cn_etf_history(
        self,
        instrument: Instrument,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        ak_module = self._akshare
        try:
            raw_data = self._call_with_retry(
                ak_module.fund_etf_hist_em,
                symbol=instrument.symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )
        except MarketDataFetchError:
            raw_data = pd.DataFrame()

        if raw_data is not None and not raw_data.empty:
            return raw_data

        return self._call_with_retry(
            ak_module.fund_etf_hist_sina,
            symbol=_format_sina_etf_symbol(instrument.symbol),
        )

    def _call_with_retry(
        self,
        func: Callable[..., pd.DataFrame],
        **kwargs: object,
    ) -> pd.DataFrame:
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                return func(**kwargs)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self._retries:
                    break
                if self._retry_delay_seconds > 0:
                    time.sleep(self._retry_delay_seconds)

        raise MarketDataFetchError(
            f"AKShare call failed after {self._retries} attempts."
        ) from last_error

    def _filter_by_date(
        self,
        history: pd.DataFrame,
        start_date: DateLike,
        end_date: DateLike,
    ) -> pd.DataFrame:
        start = _to_timestamp(start_date)
        end = _to_timestamp(end_date)
        filtered = history[(history["date"] >= start) & (history["date"] <= end)]
        return filtered.reset_index(drop=True)

    def _resolve_asset_type(self, asset_type: AssetType | str) -> AssetType:
        try:
            return AssetType(asset_type)
        except ValueError as exc:
            raise UnsupportedAssetTypeError(
                f"Unsupported asset type: {asset_type!r}"
            ) from exc

    @property
    def _akshare(self) -> Any:
        if self._ak_module is None:
            import akshare as ak

            self._ak_module = ak
        return self._ak_module


def _format_akshare_date(value: DateLike) -> str:
    return _to_timestamp(value).strftime("%Y%m%d")


def _to_timestamp(value: DateLike) -> pd.Timestamp:
    return pd.to_datetime(value, errors="raise").normalize()


def _format_sina_etf_symbol(symbol: str) -> str:
    normalized = symbol.lower()
    if normalized.startswith(("sh", "sz")):
        return normalized
    if normalized.startswith("5"):
        return f"sh{normalized}"
    if normalized.startswith("1"):
        return f"sz{normalized}"
    return normalized


def _format_cn_index_symbol(symbol: str) -> str:
    normalized = symbol.lower()
    if normalized.startswith(("sh", "sz")):
        return normalized
    if normalized.startswith("399"):
        return f"sz{normalized}"
    return f"sh{normalized}"


def _strip_exchange_prefix(symbol: str) -> str:
    normalized = symbol.lower()
    if normalized.startswith(("sh", "sz")):
        return normalized[2:]
    return symbol


def _read_realtime_float(row: pd.Series, column: str) -> float | None:
    if column not in row:
        return None
    value = pd.to_numeric(row[column], errors="coerce")
    if pd.isna(value):
        return None
    return float(value)
