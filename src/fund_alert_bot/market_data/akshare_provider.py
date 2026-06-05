"""AKShare-backed market data provider."""

from __future__ import annotations

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
    ) -> None:
        if retries < 1:
            raise ValueError("retries must be at least 1")

        self._ak_module = ak_module
        self._retries = retries
        self._retry_delay_seconds = retry_delay_seconds

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

        if asset_type is AssetType.CN_OPEN_FUND:
            history = self._filter_by_date(history, start_date, end_date)

        if history.empty:
            raise EmptyMarketDataError(
                f"No market data returned for {instrument.symbol} "
                f"between {start_date} and {end_date}."
            )
        return history[NORMALIZED_COLUMNS]

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        """Return the last normalized historical row for now."""

        end_date = date.today()
        start_date = end_date - timedelta(days=365 * 30)
        try:
            history = self.get_history(instrument, start_date, end_date)
        except EmptyMarketDataError:
            return None

        latest_row = history.iloc[-1].to_dict()
        return {
            key: None if pd.isna(value) else value for key, value in latest_row.items()
        }

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
                ak_module.index_zh_a_hist,
                symbol=instrument.symbol,
                period="daily",
                start_date=start,
                end_date=end,
            )
        if asset_type is AssetType.CN_ETF:
            return self._call_with_retry(
                ak_module.fund_etf_hist_em,
                symbol=instrument.symbol,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="",
            )
        if asset_type is AssetType.CN_STOCK:
            return self._call_with_retry(
                ak_module.stock_zh_a_hist,
                symbol=instrument.symbol,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
        if asset_type is AssetType.CN_OPEN_FUND:
            return self._call_with_retry(
                ak_module.fund_open_fund_info_em,
                symbol=instrument.symbol,
                indicator="单位净值走势",
            )

        raise UnsupportedAssetTypeError(f"Unsupported asset type: {asset_type!r}")

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
