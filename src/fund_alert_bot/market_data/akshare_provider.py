"""AKShare-backed market data provider."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from fund_alert_bot.market_data.exceptions import (
    EmptyMarketDataError,
    MarketDataFetchError,
    UnsupportedAssetTypeError,
)
from fund_alert_bot.market_data.models import (
    AssetType,
    FundNav,
    Instrument,
    PriceBasis,
    RealtimeQuote,
)
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
        fund_nav_cache_ttl_seconds: float = 300.0,
        eastmoney_failure_ttl_seconds: float = 30.0,
        today_factory: Callable[[], date] = date.today,
        now_factory: Callable[[], datetime] | None = None,
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
        if (
            not math.isfinite(fund_nav_cache_ttl_seconds)
            or fund_nav_cache_ttl_seconds < 0
        ):
            raise ValueError("fund_nav_cache_ttl_seconds must be non-negative")
        if (
            not math.isfinite(eastmoney_failure_ttl_seconds)
            or eastmoney_failure_ttl_seconds < 0
        ):
            raise ValueError("eastmoney_failure_ttl_seconds must be non-negative")

        self._ak_module = ak_module
        self._retries = retries
        self._retry_delay_seconds = retry_delay_seconds
        self._latest_lookback_days = latest_lookback_days
        self._realtime_spot_ttl_seconds = realtime_spot_ttl_seconds
        self._fund_nav_cache_ttl_seconds = fund_nav_cache_ttl_seconds
        self._eastmoney_failure_ttl_seconds = eastmoney_failure_ttl_seconds
        self._today_factory = today_factory
        self._now_factory = now_factory or (lambda: datetime.now(UTC))
        self._realtime_spot_cache: dict[
            AssetType, tuple[float, datetime, pd.DataFrame]
        ] = {}
        self._sina_etf_spot_cache: (
            tuple[float, datetime, pd.DataFrame | None] | None
        ) = None
        self._fund_nav_cache: dict[str, tuple[float, pd.DataFrame | None]] = {}
        self._eastmoney_failed_at: dict[str, float] = {}

    def get_history(
        self,
        instrument: Instrument,
        start_date: DateLike,
        end_date: DateLike,
        *,
        price_basis: PriceBasis = PriceBasis.UNADJUSTED,
    ) -> pd.DataFrame:
        """Return normalized daily history for an instrument."""

        asset_type = self._resolve_asset_type(instrument.asset_type)
        basis = self._resolve_price_basis(price_basis)
        raw_data, source = self._fetch_raw_history(
            instrument,
            asset_type,
            start_date,
            end_date,
            basis,
        )
        history = normalize_history(raw_data, asset_type, source=source)
        history = self._filter_by_date(history, start_date, end_date)

        if history.empty:
            raise EmptyMarketDataError(
                f"No market data returned for {instrument.symbol} "
                f"between {start_date} and {end_date}."
            )
        result = history[NORMALIZED_COLUMNS].copy()
        result.attrs.update(
            {
                "symbol": _strip_exchange_prefix(instrument.symbol),
                "source": source,
                "price_basis": basis.value,
                "frequency": "daily",
            }
        )
        return result

    def get_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        """Return an ETF quote from Eastmoney or the Sina pre-alert fallback."""

        if self._resolve_asset_type(instrument.asset_type) is not AssetType.CN_ETF:
            raise UnsupportedAssetTypeError("Realtime plan quotes require cn_etf.")

        symbol = _strip_exchange_prefix(instrument.symbol)
        snapshot = self._fetch_raw_realtime(AssetType.CN_ETF)
        if snapshot is not None:
            raw_data, fetched_at = snapshot
            row = _find_realtime_row(raw_data, symbol)
            if row is not None and _read_realtime_float(row, "最新价") is not None:
                return self._build_etf_quote(
                    row,
                    symbol=symbol,
                    source="eastmoney",
                    fetched_at=fetched_at,
                )

        return self.get_sina_etf_realtime_quote(instrument)

    def get_sina_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        """Return the Sina fallback separately after Eastmoney validation fails."""

        if self._resolve_asset_type(instrument.asset_type) is not AssetType.CN_ETF:
            raise UnsupportedAssetTypeError("Realtime plan quotes require cn_etf.")
        symbol = _strip_exchange_prefix(instrument.symbol)
        sina_data, fetched_at = self._fetch_sina_etf_realtime()
        sina_row = _find_realtime_row(
            sina_data,
            _format_sina_etf_symbol(symbol),
        )
        if sina_row is None:
            raise EmptyMarketDataError(f"No realtime ETF quote returned for {symbol}.")
        return self._build_etf_quote(
            sina_row,
            symbol=symbol,
            source="sina_fallback",
            fetched_at=fetched_at,
        )

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

    def get_fund_nav(
        self,
        instrument: Instrument,
        nav_date: DateLike | None = None,
    ) -> FundNav:
        """Return one positive, finite feeder-fund unit NAV with its exact date."""

        if (
            self._resolve_asset_type(instrument.asset_type)
            is not AssetType.CN_OPEN_FUND
        ):
            raise UnsupportedAssetTypeError("Unit NAV requires cn_open_fund.")

        symbol = _strip_exchange_prefix(instrument.symbol)
        history = normalize_history(
            self._fetch_open_fund_history(symbol),
            AssetType.CN_OPEN_FUND,
            source="akshare_eastmoney",
        )
        history = history.loc[history["date"] <= _to_timestamp(self._today_factory())]
        if nav_date is not None:
            expected = _to_timestamp(nav_date)
            history = history.loc[history["date"] == expected]
        if history.empty:
            expected_text = (
                "latest" if nav_date is None else str(_to_timestamp(nav_date).date())
            )
            raise EmptyMarketDataError(
                f"No unit NAV returned for {symbol} on {expected_text}."
            )

        row = history.iloc[-1]
        value = pd.to_numeric(row["close"], errors="coerce")
        if pd.isna(value) or not math.isfinite(float(value)) or float(value) <= 0:
            raise EmptyMarketDataError(
                f"Unit NAV for {symbol} on {row['date'].date()} is invalid."
            )
        return FundNav(
            symbol=symbol,
            date=row["date"].date(),
            value=float(value),
            source="akshare_eastmoney",
        )

    def get_fund_type(self, symbol: str) -> str:
        """Return one fund's declared type from the per-symbol Xueqiu endpoint."""

        frame = self._call_with_retry(
            self._akshare.fund_individual_basic_info_xq,
            symbol=_strip_exchange_prefix(symbol),
            timeout=10,
        )
        if frame.empty or not {"item", "value"}.issubset(frame.columns):
            raise EmptyMarketDataError("Fund metadata is empty or malformed.")
        matches = frame.loc[
            frame["item"].astype(str).str.strip() == "基金类型", "value"
        ]
        if matches.empty or not str(matches.iloc[0]).strip():
            raise EmptyMarketDataError("Fund metadata has no declared fund type.")
        return str(matches.iloc[0]).strip()

    def _get_realtime_latest(
        self,
        instrument: Instrument,
    ) -> dict[str, object] | None:
        asset_type = self._resolve_asset_type(instrument.asset_type)
        if asset_type is AssetType.CN_OPEN_FUND:
            return None

        snapshot = self._fetch_raw_realtime(asset_type)
        if snapshot is None:
            return None
        raw_data, _fetched_at = snapshot
        if raw_data.empty or "代码" not in raw_data.columns:
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
            "open": _read_first_realtime_float(row, "开盘价", "今开"),
            "high": _read_first_realtime_float(row, "最高价", "最高"),
            "low": _read_first_realtime_float(row, "最低价", "最低"),
            "close": close,
            "volume": _read_realtime_float(row, "成交量"),
            "amount": _read_realtime_float(row, "成交额"),
            "source": "akshare_realtime",
        }

    def _build_etf_quote(
        self,
        row: pd.Series,
        *,
        symbol: str,
        source: str,
        fetched_at: datetime,
    ) -> RealtimeQuote:
        return RealtimeQuote(
            symbol=symbol,
            price=_read_realtime_float(row, "最新价"),
            previous_close=_read_realtime_float(row, "昨收"),
            volume=_read_realtime_float(row, "成交量"),
            amount=_read_realtime_float(row, "成交额"),
            source=source,
            fetched_at=fetched_at,
        )

    def _fetch_raw_realtime(
        self,
        asset_type: AssetType,
    ) -> tuple[pd.DataFrame, datetime] | None:
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
            raw_data = pd.DataFrame()

        fetched_at = self._now_factory()
        self._write_realtime_spot_cache(asset_type, raw_data, fetched_at)
        return raw_data, fetched_at

    def _read_realtime_spot_cache(
        self,
        asset_type: AssetType,
    ) -> tuple[pd.DataFrame, datetime] | None:
        if self._realtime_spot_ttl_seconds <= 0:
            return None

        cached = self._realtime_spot_cache.get(asset_type)
        if cached is None:
            return None

        cached_at, fetched_at, raw_data = cached
        if time.monotonic() - cached_at <= self._realtime_spot_ttl_seconds:
            return raw_data, fetched_at

        self._realtime_spot_cache.pop(asset_type, None)
        return None

    def _write_realtime_spot_cache(
        self,
        asset_type: AssetType,
        raw_data: pd.DataFrame,
        fetched_at: datetime,
    ) -> None:
        if self._realtime_spot_ttl_seconds <= 0:
            return
        self._realtime_spot_cache[asset_type] = (
            time.monotonic(),
            fetched_at,
            raw_data,
        )

    def _fetch_raw_history(
        self,
        instrument: Instrument,
        asset_type: AssetType,
        start_date: DateLike,
        end_date: DateLike,
        price_basis: PriceBasis,
    ) -> tuple[pd.DataFrame, str]:
        start = _format_akshare_date(start_date)
        end = _format_akshare_date(end_date)
        ak_module = self._akshare

        if (
            asset_type is not AssetType.CN_ETF
            and price_basis is not PriceBasis.UNADJUSTED
        ):
            raise UnsupportedAssetTypeError(
                "Adjusted price history is currently supported only for cn_etf."
            )

        if asset_type is AssetType.CN_INDEX:
            return (
                self._call_with_retry(
                    ak_module.stock_zh_index_daily_em,
                    symbol=_format_cn_index_symbol(instrument.symbol),
                ),
                "akshare",
            )
        if asset_type is AssetType.CN_ETF:
            return self._fetch_cn_etf_history(
                instrument,
                start,
                end,
                price_basis=price_basis,
            )
        if asset_type is AssetType.CN_STOCK:
            return (
                self._call_with_retry(
                    ak_module.stock_zh_a_hist,
                    symbol=instrument.symbol,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust="",
                ),
                "akshare",
            )
        if asset_type is AssetType.CN_OPEN_FUND:
            return self._fetch_open_fund_history(instrument.symbol), "akshare_eastmoney"

        raise UnsupportedAssetTypeError(f"Unsupported asset type: {asset_type!r}")

    def _fetch_open_fund_history(self, symbol: str) -> pd.DataFrame:
        symbol = _strip_exchange_prefix(symbol)
        cached = self._fund_nav_cache.get(symbol)
        if cached is not None:
            cached_at, raw_data = cached
            if time.monotonic() - cached_at <= self._fund_nav_cache_ttl_seconds:
                if raw_data is None:
                    raise MarketDataFetchError(
                        f"Recent unit NAV request for {symbol} failed; "
                        "retry suppressed."
                    )
                return raw_data
            self._fund_nav_cache.pop(symbol, None)

        try:
            raw_data = self._call_eastmoney_with_retry(
                self._akshare.fund_open_fund_info_em,
                failure_group="fund_nav",
                symbol=symbol,
                indicator="\u5355\u4f4d\u51c0\u503c\u8d70\u52bf",
            )
        except MarketDataFetchError:
            if self._fund_nav_cache_ttl_seconds > 0:
                self._fund_nav_cache[symbol] = (time.monotonic(), None)
            raise
        if self._fund_nav_cache_ttl_seconds > 0:
            self._fund_nav_cache[symbol] = (time.monotonic(), raw_data)
        return raw_data

    def _fetch_cn_etf_history(
        self,
        instrument: Instrument,
        start_date: str,
        end_date: str,
        *,
        price_basis: PriceBasis,
    ) -> tuple[pd.DataFrame, str]:
        ak_module = self._akshare
        adjust = "qfq" if price_basis is PriceBasis.QFQ else ""
        if price_basis is PriceBasis.QFQ:
            return (
                self._call_eastmoney_with_retry(
                    ak_module.fund_etf_hist_em,
                    failure_group="etf_history",
                    symbol=instrument.symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                ),
                "akshare_eastmoney",
            )

        try:
            raw_data = self._call_eastmoney_with_retry(
                ak_module.fund_etf_hist_em,
                failure_group="etf_history",
                symbol=instrument.symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        except MarketDataFetchError:
            raw_data = pd.DataFrame()

        if raw_data is not None and not raw_data.empty:
            return raw_data, "akshare"

        return (
            self._call_with_retry(
                ak_module.fund_etf_hist_sina,
                symbol=_format_sina_etf_symbol(instrument.symbol),
            ),
            "akshare",
        )

    def _fetch_sina_etf_realtime(self) -> tuple[pd.DataFrame, datetime]:
        if self._sina_etf_spot_cache is not None:
            cached_at, fetched_at, raw_data = self._sina_etf_spot_cache
            if time.monotonic() - cached_at <= self._realtime_spot_ttl_seconds:
                if raw_data is None:
                    raise MarketDataFetchError(
                        "Recent Sina realtime request failed; retry suppressed."
                    )
                return raw_data, fetched_at

        try:
            raw_data = self._call_with_retry(
                self._akshare.fund_etf_category_sina,
                symbol="ETF\u57fa\u91d1",
            )
        except MarketDataFetchError:
            if self._realtime_spot_ttl_seconds > 0:
                self._sina_etf_spot_cache = (
                    time.monotonic(),
                    self._now_factory(),
                    None,
                )
            raise
        fetched_at = self._now_factory()
        if self._realtime_spot_ttl_seconds > 0:
            self._sina_etf_spot_cache = (time.monotonic(), fetched_at, raw_data)
        return raw_data, fetched_at

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

    def _call_eastmoney_with_retry(
        self,
        func: Callable[..., pd.DataFrame],
        *,
        failure_group: str,
        **kwargs: object,
    ) -> pd.DataFrame:
        now = time.monotonic()
        failed_at = self._eastmoney_failed_at.get(failure_group)
        if failed_at is not None and (
            now - failed_at <= self._eastmoney_failure_ttl_seconds
        ):
            raise MarketDataFetchError(
                "Recent Eastmoney request failed; retry suppressed."
            )
        try:
            result = self._call_with_retry(func, **kwargs)
        except MarketDataFetchError:
            if self._eastmoney_failure_ttl_seconds > 0:
                self._eastmoney_failed_at[failure_group] = time.monotonic()
            raise
        self._eastmoney_failed_at.pop(failure_group, None)
        return result

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

    def _resolve_price_basis(self, price_basis: PriceBasis | str) -> PriceBasis:
        try:
            return PriceBasis(price_basis)
        except ValueError as exc:
            raise ValueError(f"Unsupported price basis: {price_basis!r}") from exc

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


def _read_first_realtime_float(row: pd.Series, *columns: str) -> float | None:
    for column in columns:
        value = _read_realtime_float(row, column)
        if value is not None:
            return value
    return None


def _find_realtime_row(raw_data: pd.DataFrame | None, symbol: str) -> pd.Series | None:
    if raw_data is None or raw_data.empty or "\u4ee3\u7801" not in raw_data.columns:
        return None
    normalized = symbol.lower()
    symbols = raw_data["\u4ee3\u7801"].astype(str).str.lower()
    matched = raw_data.loc[symbols == normalized]
    if matched.empty:
        return None
    return matched.iloc[0]
