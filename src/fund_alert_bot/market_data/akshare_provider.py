"""AKShare-backed market data provider."""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from fund_alert_bot.market_data.exceptions import (
    EmptyMarketDataError,
    MarketDataFetchError,
    MarketDataNormalizeError,
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

_EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbol}"
_REALTIME_HTTP_TIMEOUT_SECONDS = 8
_CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)


class AkshareMarketDataProvider(MarketDataProvider):
    """Fetch and normalize historical market data from AKShare."""

    def __init__(
        self,
        *,
        ak_module: Any | None = None,
        retries: int = 3,
        retry_delay_seconds: float = 0.5,
        latest_lookback_days: int = 45,
        history_cache_ttl_seconds: float = 300.0,
        eastmoney_retries: int | None = None,
        realtime_spot_ttl_seconds: float = 30.0,
        fund_nav_cache_ttl_seconds: float = 300.0,
        eastmoney_failure_ttl_seconds: float = 30.0,
        today_factory: Callable[[], date] = date.today,
        now_factory: Callable[[], datetime] | None = None,
        http_get: Callable[..., Any] | None = None,
    ) -> None:
        if retries < 1:
            raise ValueError("retries must be at least 1")
        if latest_lookback_days < 1:
            raise ValueError("latest_lookback_days must be at least 1")
        if eastmoney_retries is not None and eastmoney_retries < 1:
            raise ValueError("eastmoney_retries must be at least 1")
        if (
            not math.isfinite(history_cache_ttl_seconds)
            or history_cache_ttl_seconds < 0
        ):
            raise ValueError("history_cache_ttl_seconds must be non-negative")
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
        self._eastmoney_retries = (
            retries if eastmoney_retries is None else eastmoney_retries
        )
        self._retry_delay_seconds = retry_delay_seconds
        self._latest_lookback_days = latest_lookback_days
        self._history_cache_ttl_seconds = history_cache_ttl_seconds
        self._realtime_spot_ttl_seconds = realtime_spot_ttl_seconds
        self._fund_nav_cache_ttl_seconds = fund_nav_cache_ttl_seconds
        self._eastmoney_failure_ttl_seconds = eastmoney_failure_ttl_seconds
        self._today_factory = today_factory
        self._now_factory = now_factory or (lambda: datetime.now(UTC))
        self._http_get = http_get or requests.get
        self._history_cache: dict[
            tuple[str, str, str, str, str], tuple[float, pd.DataFrame]
        ] = {}
        self._etf_quote_cache: dict[tuple[str, str], tuple[float, RealtimeQuote]] = {}
        self._fund_nav_cache: dict[str, tuple[float, pd.DataFrame | None]] = {}
        self._eastmoney_failed_at: float | None = None
        # Keep Sina cooldowns isolated per symbol. A transient failure for one
        # ETF must not suppress fallback quotes for every other plan.
        self._sina_failed_at: dict[str, float] = {}
        self._request_counts: dict[str, int] = {}

    def request_counts(self) -> dict[str, int]:
        """Return provider-visible logical attempts.

        Proxy-internal retries are not counted.
        """

        return self._request_counts.copy()

    def _count_request(self, kind: str) -> None:
        self._request_counts[kind] = self._request_counts.get(kind, 0) + 1

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
        cache_key = (
            _strip_exchange_prefix(instrument.symbol),
            asset_type.value,
            _format_akshare_date(start_date),
            _format_akshare_date(end_date),
            basis.value,
        )
        cached = self._read_history_cache(cache_key)
        if cached is not None:
            LOGGER.debug("AKShare history cache hit key=%s", cache_key)
            return cached

        raw_data, source, eastmoney_request_executed = self._fetch_raw_history(
            instrument,
            asset_type,
            start_date,
            end_date,
            basis,
        )
        history = self._normalize_history(
            raw_data,
            asset_type,
            source=source,
            eastmoney_request_executed=eastmoney_request_executed,
        )
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
        self._write_history_cache(cache_key, result)
        return result

    def _read_history_cache(
        self,
        cache_key: tuple[str, str, str, str, str],
    ) -> pd.DataFrame | None:
        if self._history_cache_ttl_seconds <= 0:
            return None
        cached = self._history_cache.get(cache_key)
        if cached is not None:
            cached_at, history = cached
            if time.monotonic() - cached_at <= self._history_cache_ttl_seconds:
                return history.copy(deep=True)
            self._history_cache.pop(cache_key, None)

        # A wider request already fetched for the same instrument and basis can
        # satisfy a narrower request without another paid provider call.
        for candidate_key, (cached_at, history) in tuple(self._history_cache.items()):
            if (
                candidate_key[0] != cache_key[0]
                or candidate_key[1] != cache_key[1]
                or candidate_key[4] != cache_key[4]
            ):
                continue
            if time.monotonic() - cached_at > self._history_cache_ttl_seconds:
                self._history_cache.pop(candidate_key, None)
                continue
            if candidate_key[2] > cache_key[2] or candidate_key[3] < cache_key[3]:
                continue
            start = pd.Timestamp(cache_key[2])
            end = pd.Timestamp(cache_key[3])
            narrowed = history.loc[
                (history["date"] >= start) & (history["date"] <= end)
            ]
            if not narrowed.empty:
                return narrowed.reset_index(drop=True).copy(deep=True)
        return None

    def _write_history_cache(
        self,
        cache_key: tuple[str, str, str, str, str],
        history: pd.DataFrame,
    ) -> None:
        if self._history_cache_ttl_seconds > 0:
            self._history_cache[cache_key] = (
                time.monotonic(),
                history.copy(deep=True),
            )

    def get_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        """Return one bounded per-symbol ETF quote with a Sina fallback."""

        if self._resolve_asset_type(instrument.asset_type) is not AssetType.CN_ETF:
            raise UnsupportedAssetTypeError("Realtime plan quotes require cn_etf.")

        symbol = _strip_exchange_prefix(instrument.symbol)
        try:
            return self._get_eastmoney_etf_quote(instrument)
        except MarketDataFetchError as eastmoney_error:
            LOGGER.warning(
                "Eastmoney realtime ETF quote unavailable; trying Sina fallback "
                "symbol=%s reason=%s",
                symbol,
                eastmoney_error,
            )
            try:
                return self.get_sina_etf_realtime_quote(instrument)
            except MarketDataFetchError as sina_error:
                raise MarketDataFetchError(
                    "Eastmoney realtime ETF quote unavailable "
                    f"({eastmoney_error}); Sina fallback unavailable "
                    f"({sina_error})."
                ) from sina_error

    def get_sina_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        """Return the Sina fallback separately after Eastmoney validation fails."""

        if self._resolve_asset_type(instrument.asset_type) is not AssetType.CN_ETF:
            raise UnsupportedAssetTypeError("Realtime plan quotes require cn_etf.")
        symbol = _strip_exchange_prefix(instrument.symbol)
        cached = self._read_etf_quote_cache("sina", symbol)
        if cached is not None:
            return cached
        failed_at = self._sina_failed_at.get(symbol)
        self._raise_if_source_cooling_down("Sina", failed_at)
        try:
            self._count_request("sina:realtime")
            response = self._http_get(
                _SINA_QUOTE_URL.format(symbol=_format_sina_etf_symbol(symbol)),
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=_REALTIME_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            quote = _parse_sina_etf_quote(response, symbol)
        except Exception as exc:  # noqa: BLE001
            self._sina_failed_at[symbol] = time.monotonic()
            raise MarketDataFetchError("Sina realtime ETF quote failed.") from exc
        self._sina_failed_at.pop(symbol, None)
        self._write_etf_quote_cache("sina", symbol, quote)
        return quote

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
        raw_data, eastmoney_request_executed = self._fetch_open_fund_history(symbol)
        history = self._normalize_history(
            raw_data,
            AssetType.CN_OPEN_FUND,
            source="akshare_eastmoney",
            eastmoney_request_executed=eastmoney_request_executed,
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
            if eastmoney_request_executed:
                self._mark_eastmoney_failure()
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
            request_kind="xueqiu:metadata",
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

        if asset_type is AssetType.CN_ETF:
            try:
                quote = self.get_etf_realtime_quote(instrument)
            except MarketDataFetchError:
                return None
            return {
                "date": pd.Timestamp(quote.fetched_at.astimezone(_CN_TIMEZONE).date()),
                "open": None,
                "high": None,
                "low": None,
                "close": quote.price,
                "volume": quote.volume,
                "amount": quote.amount,
                "source": f"{quote.source}_realtime",
            }

        try:
            quote = self._get_eastmoney_symbol_quote(instrument)
        except MarketDataFetchError:
            return None
        return {
            "date": pd.Timestamp(quote.fetched_at.astimezone(_CN_TIMEZONE).date()),
            "open": None,
            "high": None,
            "low": None,
            "close": quote.price,
            "volume": quote.volume,
            "amount": quote.amount,
            "source": f"{quote.source}_realtime",
        }

    def _get_eastmoney_etf_quote(self, instrument: Instrument) -> RealtimeQuote:
        symbol = _strip_exchange_prefix(instrument.symbol)
        try:
            market_id = _eastmoney_etf_market_id(symbol)
        except ValueError as exc:
            raise MarketDataFetchError(
                "Eastmoney realtime ETF quote has an unsupported symbol."
            ) from exc
        return self._get_eastmoney_quote(instrument, market_id=market_id)

    def _get_eastmoney_symbol_quote(self, instrument: Instrument) -> RealtimeQuote:
        """Fetch one index/stock quote without AKShare full-market pagination."""
        return self._get_eastmoney_quote(
            instrument,
            market_id=_eastmoney_market_id(instrument.asset_type, instrument.symbol),
        )

    def _get_eastmoney_quote(
        self,
        instrument: Instrument,
        *,
        market_id: int,
    ) -> RealtimeQuote:
        symbol = _strip_exchange_prefix(instrument.symbol)
        asset_type = AssetType(instrument.asset_type)
        cache_source = f"eastmoney_{asset_type.value}"
        cached = self._read_etf_quote_cache(cache_source, symbol)
        if cached is not None:
            return cached
        self._raise_if_source_cooling_down("Eastmoney", self._eastmoney_failed_at)
        try:
            self._count_request("eastmoney:realtime")
            response = self._http_get(
                _EASTMONEY_QUOTE_URL,
                params={
                    "fltt": "2",
                    "invt": "2",
                    "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f86",
                    "secid": f"{market_id}.{symbol}",
                },
                timeout=_REALTIME_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            self._eastmoney_failed_at = time.monotonic()
            raise MarketDataFetchError("Eastmoney realtime quote failed.") from exc
        try:
            quote = _parse_eastmoney_quote(payload, symbol)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise MarketDataFetchError(
                "Eastmoney realtime quote has no usable data."
            ) from exc
        self._eastmoney_failed_at = None
        self._write_etf_quote_cache(cache_source, symbol, quote)
        return quote

    def _read_etf_quote_cache(
        self,
        source: str,
        symbol: str,
    ) -> RealtimeQuote | None:
        cached = self._etf_quote_cache.get((source, symbol))
        if cached is None:
            return None
        cached_at, quote = cached
        if time.monotonic() - cached_at <= self._realtime_spot_ttl_seconds:
            return quote
        self._etf_quote_cache.pop((source, symbol), None)
        return None

    def _write_etf_quote_cache(
        self,
        source: str,
        symbol: str,
        quote: RealtimeQuote,
    ) -> None:
        if self._realtime_spot_ttl_seconds > 0:
            self._etf_quote_cache[(source, symbol)] = (time.monotonic(), quote)

    def _raise_if_source_cooling_down(
        self,
        source: str,
        failed_at: float | None,
    ) -> None:
        if failed_at is not None and (
            time.monotonic() - failed_at <= self._eastmoney_failure_ttl_seconds
        ):
            raise MarketDataFetchError(
                f"Recent {source} request failed; retry suppressed."
            )

    def _mark_eastmoney_failure(self) -> None:
        if self._eastmoney_failure_ttl_seconds > 0:
            self._eastmoney_failed_at = time.monotonic()

    def _normalize_history(
        self,
        raw_data: pd.DataFrame,
        asset_type: AssetType,
        *,
        source: str,
        eastmoney_request_executed: bool,
    ) -> pd.DataFrame:
        try:
            return normalize_history(raw_data, asset_type, source=source)
        except (EmptyMarketDataError, MarketDataNormalizeError):
            if eastmoney_request_executed:
                self._mark_eastmoney_failure()
            raise

    def _fetch_raw_history(
        self,
        instrument: Instrument,
        asset_type: AssetType,
        start_date: DateLike,
        end_date: DateLike,
        price_basis: PriceBasis,
    ) -> tuple[pd.DataFrame, str, bool]:
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
                self._call_eastmoney_with_retry(
                    ak_module.stock_zh_index_daily_em,
                    symbol=_format_cn_index_symbol(instrument.symbol),
                ),
                "akshare",
                True,
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
                self._call_eastmoney_with_retry(
                    ak_module.stock_zh_a_hist,
                    symbol=instrument.symbol,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust="",
                ),
                "akshare",
                True,
            )
        if asset_type is AssetType.CN_OPEN_FUND:
            raw_data, eastmoney_request_executed = self._fetch_open_fund_history(
                instrument.symbol
            )
            return raw_data, "akshare_eastmoney", eastmoney_request_executed

        raise UnsupportedAssetTypeError(f"Unsupported asset type: {asset_type!r}")

    def _fetch_open_fund_history(self, symbol: str) -> tuple[pd.DataFrame, bool]:
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
                return raw_data, False
            self._fund_nav_cache.pop(symbol, None)

        try:
            raw_data = self._call_eastmoney_with_retry(
                self._akshare.fund_open_fund_info_em,
                request_kind="eastmoney:nav",
                symbol=symbol,
                indicator="\u5355\u4f4d\u51c0\u503c\u8d70\u52bf",
            )
        except MarketDataFetchError:
            if self._fund_nav_cache_ttl_seconds > 0:
                self._fund_nav_cache[symbol] = (time.monotonic(), None)
            raise
        if self._fund_nav_cache_ttl_seconds > 0:
            self._fund_nav_cache[symbol] = (time.monotonic(), raw_data)
        return raw_data, True

    def _fetch_cn_etf_history(
        self,
        instrument: Instrument,
        start_date: str,
        end_date: str,
        *,
        price_basis: PriceBasis,
    ) -> tuple[pd.DataFrame, str, bool]:
        ak_module = self._akshare
        adjust = "qfq" if price_basis is PriceBasis.QFQ else ""
        if price_basis is PriceBasis.QFQ:
            return (
                self._call_eastmoney_with_retry(
                    ak_module.fund_etf_hist_em,
                    symbol=instrument.symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                ),
                "akshare_eastmoney",
                True,
            )

        try:
            raw_data = self._call_eastmoney_with_retry(
                ak_module.fund_etf_hist_em,
                symbol=instrument.symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        except MarketDataFetchError:
            raw_data = pd.DataFrame()

        if raw_data is not None and not raw_data.empty:
            return raw_data, "akshare", True

        return (
            self._call_with_retry(
                ak_module.fund_etf_hist_sina,
                request_kind="sina:history",
                symbol=_format_sina_etf_symbol(instrument.symbol),
            ),
            "akshare",
            False,
        )

    def _call_with_retry(
        self,
        func: Callable[..., pd.DataFrame],
        *,
        attempts: int | None = None,
        request_kind: str | None = None,
        **kwargs: object,
    ) -> pd.DataFrame:
        last_error: Exception | None = None
        retry_count = self._retries if attempts is None else attempts
        for attempt in range(1, retry_count + 1):
            try:
                if request_kind is not None:
                    self._count_request(request_kind)
                return func(**kwargs)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == retry_count:
                    break
                if self._retry_delay_seconds > 0:
                    time.sleep(self._retry_delay_seconds)

        raise MarketDataFetchError(
            f"AKShare call failed after {retry_count} attempts."
        ) from last_error

    def _call_eastmoney_with_retry(
        self,
        func: Callable[..., pd.DataFrame],
        *,
        request_kind: str = "eastmoney:history",
        **kwargs: object,
    ) -> pd.DataFrame:
        self._raise_if_source_cooling_down("Eastmoney", self._eastmoney_failed_at)
        try:
            result = self._call_with_retry(
                func,
                attempts=self._eastmoney_retries,
                request_kind=request_kind,
                **kwargs,
            )
        except MarketDataFetchError:
            self._mark_eastmoney_failure()
            raise
        if result is None or result.empty:
            self._mark_eastmoney_failure()
            raise MarketDataFetchError("Eastmoney returned no market data.")
        self._eastmoney_failed_at = None
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


def _eastmoney_etf_market_id(symbol: str) -> int:
    if symbol.startswith("5"):
        return 1
    if symbol.startswith("1"):
        return 0
    raise ValueError(f"Unsupported CN ETF exchange for symbol {symbol}.")


def _eastmoney_market_id(asset_type: AssetType | str, symbol: str) -> int:
    normalized = str(symbol).lower()
    if normalized.startswith("sh"):
        return 1
    if normalized.startswith("sz"):
        return 0
    normalized = _strip_exchange_prefix(normalized)
    resolved = AssetType(asset_type)
    if resolved is AssetType.CN_INDEX:
        return 0 if normalized.startswith("399") else 1
    if resolved is AssetType.CN_STOCK:
        if normalized.startswith("920"):
            return 0
        return 1 if normalized.startswith(("6", "9")) else 0
    return _eastmoney_etf_market_id(normalized)


def _parse_eastmoney_quote(
    payload: object,
    symbol: str,
) -> RealtimeQuote:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Eastmoney realtime response has no quote data.")
    data = payload["data"]
    if str(data.get("f57", "")) != symbol:
        raise ValueError("Eastmoney realtime response symbol does not match.")
    timestamp = _read_unix_timestamp(data.get("f86"))
    price = _read_positive_float(data.get("f43"), "Eastmoney latest price")
    return RealtimeQuote(
        symbol=symbol,
        price=price,
        previous_close=_read_mapping_float(data, "f60"),
        volume=_read_mapping_float(data, "f47"),
        amount=_read_mapping_float(data, "f48"),
        source="eastmoney",
        fetched_at=timestamp,
    )


def _parse_sina_etf_quote(response: Any, symbol: str) -> RealtimeQuote:
    content = getattr(response, "content", b"")
    text = (
        bytes(content).decode("gb18030")
        if content
        else str(getattr(response, "text", ""))
    )
    match = re.search(r'var hq_str_([a-z]{2}\d{6})="([^"]*)"', text)
    if match is None:
        raise ValueError("Sina realtime response has no quote data.")
    if match.group(1) != _format_sina_etf_symbol(symbol):
        raise ValueError("Sina realtime response symbol does not match.")
    values = match.group(2).split(",")
    if len(values) < 32:
        raise ValueError("Sina realtime response is incomplete.")
    quote_time = datetime.strptime(
        f"{values[30]} {values[31]}",
        "%Y-%m-%d %H:%M:%S",
    ).replace(tzinfo=_CN_TIMEZONE)
    price = _read_positive_float(values[3], "Sina latest price")
    return RealtimeQuote(
        symbol=symbol,
        price=price,
        previous_close=_read_text_float(values[2]),
        volume=_read_text_float(values[8]),
        amount=_read_text_float(values[9]),
        source="sina_fallback",
        fetched_at=quote_time,
    )


def _read_unix_timestamp(value: object) -> datetime:
    timestamp = _read_text_float(value)
    if timestamp is None or timestamp <= 0:
        raise ValueError("Realtime response has no valid timestamp.")
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _read_mapping_float(values: dict[str, object], key: str) -> float | None:
    return _read_text_float(values.get(key))


def _read_positive_float(value: object, label: str) -> float:
    number = _read_text_float(value)
    if number is None or number <= 0:
        raise ValueError(f"{label} must be positive and finite.")
    return number


def _read_text_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
