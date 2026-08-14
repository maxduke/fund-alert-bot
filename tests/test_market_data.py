from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pandas as pd
import pytest

from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    AssetType,
    EmptyMarketDataError,
    Instrument,
    MarketDataFetchError,
    MarketDataNormalizeError,
    PriceBasis,
    UnsupportedAssetTypeError,
)
from fund_alert_bot.market_data.normalize import NORMALIZED_COLUMNS


class FakeAkshare:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_etf_call = False

    def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("fund_etf_hist_em", kwargs))
        if self.fail_next_etf_call:
            self.fail_next_etf_call = False
            raise RuntimeError("temporary AKShare failure")
        return _price_history()

    def fund_open_fund_info_em(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("fund_open_fund_info_em", kwargs))
        return pd.DataFrame(
            {
                "\u51c0\u503c\u65e5\u671f": [
                    "2024-01-03",
                    "2024-01-01",
                    "2024-01-02",
                ],
                "\u5355\u4f4d\u51c0\u503c": ["1.20", "1.00", "1.10"],
            }
        )

    def fund_individual_basic_info_xq(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("fund_individual_basic_info_xq", kwargs))
        return pd.DataFrame(
            {"item": ["基金代码", "基金类型"], "value": ["110026", "指数型-股票"]}
        )

    def stock_zh_index_daily_em(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("stock_zh_index_daily_em", kwargs))
        return _english_price_history()

    def stock_zh_a_hist(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("stock_zh_a_hist", kwargs))
        return _price_history()

    def fund_etf_hist_sina(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("fund_etf_hist_sina", kwargs))
        return _english_price_history()


class FakeResponse:
    def __init__(
        self,
        *,
        json_data: object | None = None,
        content: bytes = b"",
        error: Exception | None = None,
    ) -> None:
        self._json_data = json_data
        self.content = content
        self.text = content.decode("gb18030") if content else ""
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        return self._json_data


def test_etf_history_normalizes_to_shared_schema() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument(
        symbol="510300",
        name="CSI 300 ETF",
        asset_type=AssetType.CN_ETF,
    )

    history = provider.get_history(instrument, "2024-01-01", "2024-01-03")

    assert list(history.columns) == NORMALIZED_COLUMNS
    assert history["date"].tolist() == [
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-01-02"),
    ]
    assert history["open"].tolist() == [1.0, 1.1]
    assert history["high"].tolist() == [1.3, 1.4]
    assert history["low"].tolist() == [0.9, 1.0]
    assert history["close"].tolist() == [1.2, 1.3]
    assert history["volume"].tolist() == [1000, 1100]
    assert history["amount"].tolist() == [10000, 11000]
    assert history["source"].tolist() == ["akshare", "akshare"]
    assert fake_ak.calls == [
        (
            "fund_etf_hist_em",
            {
                "symbol": "510300",
                "period": "daily",
                "start_date": "20240101",
                "end_date": "20240103",
                "adjust": "",
            },
        )
    ]


def test_history_cache_reuses_identical_request_without_mutating_cached_data() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)

    first = provider.get_history(instrument, "2024-01-01", "2024-01-03")
    first.loc[0, "close"] = 999
    second = provider.get_history(instrument, "2024-01-01", "2024-01-03")

    assert second.loc[0, "close"] == 1.2
    assert [name for name, _kwargs in fake_ak.calls] == ["fund_etf_hist_em"]


def test_history_cache_reuses_wider_request_for_narrower_range() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)

    provider.get_history(instrument, "2024-01-01", "2024-01-03")
    narrowed = provider.get_history(instrument, "2024-01-02", "2024-01-02")

    assert narrowed["date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2024-01-02",
    ]
    assert [name for name, _kwargs in fake_ak.calls] == ["fund_etf_hist_em"]


def test_fund_type_uses_single_symbol_xueqiu_metadata_request() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)

    assert provider.get_fund_type("110026") == "指数型-股票"
    assert fake_ak.calls == [
        ("fund_individual_basic_info_xq", {"symbol": "110026", "timeout": 10})
    ]


def test_drawdown_plan_history_uses_qfq_eastmoney_without_sina_fallback() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)

    history = provider.get_history(
        instrument,
        "2024-01-01",
        "2024-01-03",
        price_basis=PriceBasis.QFQ,
    )

    assert history["source"].tolist() == [
        "akshare_eastmoney",
        "akshare_eastmoney",
    ]
    assert history.attrs == {
        "symbol": "510300",
        "source": "akshare_eastmoney",
        "price_basis": "qfq",
        "frequency": "daily",
    }
    assert fake_ak.calls == [
        (
            "fund_etf_hist_em",
            {
                "symbol": "510300",
                "period": "daily",
                "start_date": "20240101",
                "end_date": "20240103",
                "adjust": "qfq",
            },
        )
    ]


def test_drawdown_plan_history_does_not_fallback_to_unadjusted_sina() -> None:
    class FailingEastmoneyAkshare(FakeAkshare):
        def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_hist_em", kwargs))
            raise RuntimeError("Eastmoney unavailable")

    fake_ak = FailingEastmoneyAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=1,
        retry_delay_seconds=0,
    )
    instrument = Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)

    with pytest.raises(MarketDataFetchError):
        provider.get_history(
            instrument,
            "2024-01-01",
            "2024-01-03",
            price_basis=PriceBasis.QFQ,
        )
    with pytest.raises(MarketDataFetchError, match="retry suppressed"):
        provider.get_history(
            Instrument("159915", "ChiNext ETF", AssetType.CN_ETF),
            "2024-01-01",
            "2024-01-03",
            price_basis=PriceBasis.QFQ,
        )

    assert [name for name, _kwargs in fake_ak.calls] == ["fund_etf_hist_em"]


def test_open_fund_history_uses_unit_nav_as_close_and_filters_by_date() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument(
        symbol="000001",
        name="Example Open Fund",
        asset_type=AssetType.CN_OPEN_FUND,
    )

    history = provider.get_history(instrument, "2024-01-02", "2024-01-03")

    assert list(history.columns) == NORMALIZED_COLUMNS
    assert history["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert history["close"].tolist() == [1.1, 1.2]
    for column in ["open", "high", "low", "volume", "amount"]:
        assert history[column].tolist() == [None, None]
    assert history["source"].tolist() == [
        "akshare_eastmoney",
        "akshare_eastmoney",
    ]
    assert fake_ak.calls == [
        (
            "fund_open_fund_info_em",
            {
                "symbol": "000001",
                "indicator": "\u5355\u4f4d\u51c0\u503c\u8d70\u52bf",
            },
        )
    ]


def test_index_history_formats_exchange_symbol_and_normalizes() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument(
        symbol="399006",
        name="ChiNext Index",
        asset_type=AssetType.CN_INDEX,
    )

    history = provider.get_history(instrument, "2024-01-01", "2024-01-03")

    assert list(history.columns) == NORMALIZED_COLUMNS
    assert history["close"].tolist() == [2.2, 2.3]
    assert fake_ak.calls == [("stock_zh_index_daily_em", {"symbol": "sz399006"})]


def test_stock_history_uses_a_share_history_and_normalizes() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument(
        symbol="300750",
        name="CATL",
        asset_type=AssetType.CN_STOCK,
    )

    history = provider.get_history(instrument, "2024-01-01", "2024-01-03")

    assert list(history.columns) == NORMALIZED_COLUMNS
    assert history["close"].tolist() == [1.2, 1.3]
    assert fake_ak.calls == [
        (
            "stock_zh_a_hist",
            {
                "symbol": "300750",
                "period": "daily",
                "start_date": "20240101",
                "end_date": "20240103",
                "adjust": "",
            },
        )
    ]


def test_etf_history_falls_back_to_sina_when_eastmoney_fails() -> None:
    class FailingEastmoneyAkshare(FakeAkshare):
        def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_hist_em", kwargs))
            raise RuntimeError("EastMoney is unavailable")

    fake_ak = FailingEastmoneyAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=2,
        retry_delay_seconds=0,
    )
    instrument = Instrument(
        symbol="510300",
        name="CSI 300 ETF",
        asset_type=AssetType.CN_ETF,
    )

    history = provider.get_history(instrument, "2024-01-01", "2024-01-03")

    assert list(history.columns) == NORMALIZED_COLUMNS
    assert history["date"].tolist() == [
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-01-02"),
    ]
    assert history["close"].tolist() == [2.2, 2.3]
    assert history["source"].tolist() == ["akshare", "akshare"]
    assert fake_ak.calls == [
        (
            "fund_etf_hist_em",
            {
                "symbol": "510300",
                "period": "daily",
                "start_date": "20240101",
                "end_date": "20240103",
                "adjust": "",
            },
        ),
        (
            "fund_etf_hist_em",
            {
                "symbol": "510300",
                "period": "daily",
                "start_date": "20240101",
                "end_date": "20240103",
                "adjust": "",
            },
        ),
        ("fund_etf_hist_sina", {"symbol": "sh510300"}),
    ]


def test_get_latest_uses_the_quote_data_date_instead_of_local_today() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, kwargs))
        return _eastmoney_quote_response("510300", timestamp=1704351000)

    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retry_delay_seconds=0,
        today_factory=lambda: date(2024, 1, 7),
        http_get=http_get,
    )
    instrument = Instrument(
        symbol="510300",
        name="CSI 300 ETF",
        asset_type=AssetType.CN_ETF,
    )

    latest = provider.get_latest(instrument)

    assert latest == {
        "date": pd.Timestamp("2024-01-04"),
        "open": None,
        "high": None,
        "low": None,
        "close": 1.25,
        "volume": 1200.0,
        "amount": 15000.0,
        "source": "eastmoney_realtime",
    }
    assert len(calls) == 1


def test_get_etf_realtime_quote_uses_eastmoney_contract() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, kwargs))
        symbol = str(kwargs["params"]["secid"]).split(".")[1]
        return _eastmoney_quote_response(
            symbol,
            price=1.25 if symbol == "510300" else 2.35,
            previous_close=1.24 if symbol == "510300" else 2.34,
        )

    fetched_at = datetime(2024, 1, 4, 6, 50, tzinfo=UTC)
    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retry_delay_seconds=0,
        http_get=http_get,
    )

    quote = provider.get_etf_realtime_quote(
        Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)
    )
    second_quote = provider.get_etf_realtime_quote(
        Instrument("159915", "ChiNext ETF", AssetType.CN_ETF)
    )

    assert quote.symbol == "510300"
    assert quote.price == 1.25
    assert quote.previous_close == 1.24
    assert quote.volume == 1200
    assert quote.amount == 15000
    assert quote.source == "eastmoney"
    assert quote.fetched_at == fetched_at
    assert second_quote.symbol == "159915"
    assert second_quote.price == 2.35
    assert second_quote.fetched_at == fetched_at
    assert [call[1]["params"]["secid"] for call in calls] == [
        "1.510300",
        "0.159915",
    ]
    assert all(call[1]["timeout"] == 8 for call in calls)


def test_get_etf_realtime_quote_falls_back_to_sina() -> None:
    calls: list[str] = []

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        if "eastmoney" in url:
            raise TimeoutError("Eastmoney unavailable")
        assert kwargs["headers"]["Referer"] == "https://finance.sina.com.cn"
        return FakeResponse(content=_sina_quote_content("510300"))

    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retries=1,
        retry_delay_seconds=0,
        http_get=http_get,
    )

    quote = provider.get_etf_realtime_quote(
        Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)
    )

    assert quote.symbol == "510300"
    assert quote.source == "sina_fallback"
    assert quote.fetched_at == datetime(2024, 1, 4, 6, 50, tzinfo=UTC)
    assert len(calls) == 2


def test_invalid_eastmoney_quote_falls_back_instead_of_fabricating_freshness() -> None:
    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        if "eastmoney" in url:
            return _eastmoney_quote_response("510300", timestamp=0)
        return FakeResponse(content=_sina_quote_content("510300"))

    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retries=1,
        retry_delay_seconds=0,
        today_factory=lambda: date(2024, 1, 7),
        http_get=http_get,
    )

    latest = provider.get_latest(Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF))

    assert latest is not None
    assert latest["date"] == pd.Timestamp("2024-01-04")
    assert latest["source"] == "sina_fallback_realtime"


def test_mismatched_sina_symbol_is_rejected() -> None:
    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retries=1,
        retry_delay_seconds=0,
        http_get=lambda *args, **kwargs: FakeResponse(
            content=_sina_quote_content("159915")
        ),
    )

    with pytest.raises(MarketDataFetchError):
        provider.get_sina_etf_realtime_quote(
            Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)
        )


def test_get_latest_reuses_same_symbol_quote_within_ttl() -> None:
    calls = 0

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        return _eastmoney_quote_response("510300")

    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retry_delay_seconds=0,
        today_factory=lambda: date(2024, 1, 4),
        http_get=http_get,
    )

    first = provider.get_latest(Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF))
    second = provider.get_latest(Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF))

    assert first is not None
    assert first["close"] == 1.25
    assert second is not None
    assert second["close"] == 1.25
    assert calls == 1


def test_eastmoney_failure_cooldown_applies_across_etf_symbols() -> None:
    calls: list[str] = []

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        if "eastmoney" in url:
            raise TimeoutError("Eastmoney rate limited")
        symbol = url.rsplit("=", 1)[1][2:]
        return FakeResponse(content=_sina_quote_content(symbol))

    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retries=1,
        retry_delay_seconds=0,
        http_get=http_get,
    )

    first = provider.get_etf_realtime_quote(
        Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)
    )
    second = provider.get_etf_realtime_quote(
        Instrument("159915", "ChiNext ETF", AssetType.CN_ETF)
    )

    assert first.fetched_at == datetime(2024, 1, 4, 6, 50, tzinfo=UTC)
    assert second.fetched_at == first.fetched_at
    assert sum("eastmoney" in url for url in calls) == 1
    assert sum("sinajs" in url for url in calls) == 2


def test_etf_quotes_cache_failures_from_both_realtime_sources() -> None:
    calls: list[str] = []

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        raise TimeoutError("realtime source unavailable")

    provider = AkshareMarketDataProvider(
        ak_module=FakeAkshare(),
        retries=1,
        retry_delay_seconds=0,
        http_get=http_get,
    )

    for symbol in ("510300", "159915"):
        with pytest.raises(MarketDataFetchError):
            provider.get_etf_realtime_quote(
                Instrument(symbol, symbol, AssetType.CN_ETF)
            )

    assert len(calls) == 2


def test_get_latest_returns_last_normalized_row() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retry_delay_seconds=0,
        today_factory=lambda: date(2024, 1, 4),
    )
    instrument = Instrument(
        symbol="000001",
        name="Example Open Fund",
        asset_type=AssetType.CN_OPEN_FUND,
    )

    latest = provider.get_latest(instrument)

    assert latest == {
        "date": pd.Timestamp("2024-01-03"),
        "open": None,
        "high": None,
        "low": None,
        "close": 1.2,
        "volume": None,
        "amount": None,
        "source": "akshare_eastmoney",
    }


def test_fund_nav_requires_exact_date_and_reuses_one_eastmoney_response() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument("000001", "Example Open Fund", AssetType.CN_OPEN_FUND)

    exact = provider.get_fund_nav(instrument, "2024-01-02")
    latest = provider.get_fund_nav(instrument)

    assert (exact.date.isoformat(), exact.value, exact.source) == (
        "2024-01-02",
        1.1,
        "akshare_eastmoney",
    )
    assert (latest.date.isoformat(), latest.value) == ("2024-01-03", 1.2)
    assert fake_ak.calls == [
        (
            "fund_open_fund_info_em",
            {
                "symbol": "000001",
                "indicator": "\u5355\u4f4d\u51c0\u503c\u8d70\u52bf",
            },
        )
    ]


def test_fund_nav_fails_closed_for_missing_exact_date_or_invalid_value() -> None:
    class InvalidNavAkshare(FakeAkshare):
        def fund_open_fund_info_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_open_fund_info_em", kwargs))
            return pd.DataFrame(
                {
                    "\u51c0\u503c\u65e5\u671f": ["2024-01-02"],
                    "\u5355\u4f4d\u51c0\u503c": ["not-a-number"],
                }
            )

    provider = AkshareMarketDataProvider(
        ak_module=InvalidNavAkshare(),
        retry_delay_seconds=0,
    )
    instrument = Instrument("000001", "Example Open Fund", AssetType.CN_OPEN_FUND)

    with pytest.raises(EmptyMarketDataError):
        provider.get_fund_nav(instrument, "2024-01-01")
    with pytest.raises(EmptyMarketDataError):
        provider.get_fund_nav(instrument, "2024-01-02")


def test_fund_nav_failure_is_cached_to_avoid_repeated_eastmoney_requests() -> None:
    class FailingNavAkshare(FakeAkshare):
        def fund_open_fund_info_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_open_fund_info_em", kwargs))
            raise RuntimeError("rate limited")

    fake_ak = FailingNavAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=1,
        retry_delay_seconds=0,
    )
    instrument = Instrument("000001", "Example Open Fund", AssetType.CN_OPEN_FUND)

    with pytest.raises(MarketDataFetchError):
        provider.get_fund_nav(instrument)
    with pytest.raises(MarketDataFetchError, match="retry suppressed"):
        provider.get_fund_nav(instrument)

    assert [name for name, _kwargs in fake_ak.calls] == ["fund_open_fund_info_em"]


def test_eastmoney_failure_suppresses_other_fund_requests_in_same_run() -> None:
    class FailingNavAkshare(FakeAkshare):
        def fund_open_fund_info_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_open_fund_info_em", kwargs))
            raise RuntimeError("rate limited")

    fake_ak = FailingNavAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=1,
        retry_delay_seconds=0,
    )

    with pytest.raises(MarketDataFetchError):
        provider.get_fund_nav(Instrument("000001", "Fund A", AssetType.CN_OPEN_FUND))
    with pytest.raises(MarketDataFetchError, match="retry suppressed"):
        provider.get_fund_nav(Instrument("000002", "Fund B", AssetType.CN_OPEN_FUND))

    assert [name for name, _kwargs in fake_ak.calls] == ["fund_open_fund_info_em"]


def test_eastmoney_failure_cooldown_is_shared_across_endpoint_families() -> None:
    class FailingEtfAkshare(FakeAkshare):
        def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_hist_em", kwargs))
            raise RuntimeError("Eastmoney rate limited")

    fake_ak = FailingEtfAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=1,
        retry_delay_seconds=0,
    )

    with pytest.raises(MarketDataFetchError):
        provider.get_history(
            Instrument("510300", "ETF", AssetType.CN_ETF),
            "2024-01-01",
            "2024-01-03",
            price_basis=PriceBasis.QFQ,
        )
    with pytest.raises(MarketDataFetchError, match="retry suppressed"):
        provider.get_fund_nav(Instrument("000001", "Fund", AssetType.CN_OPEN_FUND))

    assert [name for name, _kwargs in fake_ak.calls] == ["fund_etf_hist_em"]


def test_normalization_keeps_last_duplicate_nav_for_a_date() -> None:
    class DuplicateNavAkshare(FakeAkshare):
        def fund_open_fund_info_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_open_fund_info_em", kwargs))
            return pd.DataFrame(
                {
                    "\u51c0\u503c\u65e5\u671f": ["2024-01-02", "2024-01-02"],
                    "\u5355\u4f4d\u51c0\u503c": ["1.10", "1.11"],
                }
            )

    provider = AkshareMarketDataProvider(
        ak_module=DuplicateNavAkshare(),
        retry_delay_seconds=0,
    )

    nav = provider.get_fund_nav(
        Instrument("000001", "Example Open Fund", AssetType.CN_OPEN_FUND),
        "2024-01-02",
    )

    assert nav.value == 1.11


def test_open_fund_history_empty_after_date_filter_raises() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(ak_module=fake_ak, retry_delay_seconds=0)
    instrument = Instrument(
        symbol="000001",
        name="Example Open Fund",
        asset_type=AssetType.CN_OPEN_FUND,
    )

    with pytest.raises(EmptyMarketDataError):
        provider.get_history(instrument, "2025-01-01", "2025-01-31")


def test_provider_retries_akshare_calls() -> None:
    fake_ak = FakeAkshare()
    fake_ak.fail_next_etf_call = True
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=2,
        retry_delay_seconds=0,
    )
    instrument = Instrument(
        symbol="510300",
        name="CSI 300 ETF",
        asset_type=AssetType.CN_ETF,
    )

    history = provider.get_history(instrument, "2024-01-01", "2024-01-03")

    assert len(history) == 2
    assert [call[0] for call in fake_ak.calls] == [
        "fund_etf_hist_em",
        "fund_etf_hist_em",
    ]


def test_provider_limits_eastmoney_retry_budget_separately() -> None:
    class AlwaysFailingAkshare(FakeAkshare):
        def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_hist_em", kwargs))
            raise RuntimeError("rate limited")

    fake_ak = AlwaysFailingAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=3,
        eastmoney_retries=1,
        retry_delay_seconds=0,
    )

    with pytest.raises(MarketDataFetchError):
        provider.get_history(
            Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF),
            "2024-01-01",
            "2024-01-03",
            price_basis=PriceBasis.QFQ,
        )

    assert len(fake_ak.calls) == 1


def test_history_cache_can_be_disabled() -> None:
    fake_ak = FakeAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retry_delay_seconds=0,
        history_cache_ttl_seconds=0,
    )
    instrument = Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)

    provider.get_history(instrument, "2024-01-01", "2024-01-03")
    provider.get_history(instrument, "2024-01-01", "2024-01-03")

    assert [name for name, _kwargs in fake_ak.calls] == [
        "fund_etf_hist_em",
        "fund_etf_hist_em",
    ]


def test_empty_akshare_response_raises_clear_exception() -> None:
    class EmptyAkshare(FakeAkshare):
        def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_hist_em", kwargs))
            return pd.DataFrame()

        def fund_etf_hist_sina(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_hist_sina", kwargs))
            return pd.DataFrame()

    provider = AkshareMarketDataProvider(
        ak_module=EmptyAkshare(),
        retry_delay_seconds=0,
    )
    instrument = Instrument(
        symbol="510300",
        name="CSI 300 ETF",
        asset_type=AssetType.CN_ETF,
    )

    with pytest.raises(EmptyMarketDataError):
        provider.get_history(instrument, "2024-01-01", "2024-01-03")


def test_missing_required_columns_raise_normalize_error() -> None:
    class MissingColumnAkshare(FakeAkshare):
        def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_hist_em", kwargs))
            return pd.DataFrame(
                {"\u65e5\u671f": ["2024-01-01"], "\u6536\u76d8": ["1.2"]}
            )

    provider = AkshareMarketDataProvider(
        ak_module=MissingColumnAkshare(),
        retry_delay_seconds=0,
    )
    instrument = Instrument(
        symbol="510300",
        name="CSI 300 ETF",
        asset_type=AssetType.CN_ETF,
    )

    with pytest.raises(MarketDataNormalizeError):
        provider.get_history(instrument, "2024-01-01", "2024-01-03")


@pytest.mark.parametrize("asset_type", ["crypto"])
def test_unsupported_asset_type_raises_clear_exception(asset_type: str) -> None:
    provider = AkshareMarketDataProvider(ak_module=FakeAkshare(), retry_delay_seconds=0)
    instrument = Instrument(
        symbol="UNKNOWN",
        name="Unsupported",
        asset_type=cast(AssetType, asset_type),
    )

    with pytest.raises(UnsupportedAssetTypeError):
        provider.get_history(instrument, "2024-01-01", "2024-01-03")


def _price_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "\u65e5\u671f": ["2024-01-02", "2024-01-01"],
            "\u5f00\u76d8": ["1.10", "1.00"],
            "\u6700\u9ad8": ["1.40", "1.30"],
            "\u6700\u4f4e": ["1.00", "0.90"],
            "\u6536\u76d8": ["1.30", "1.20"],
            "\u6210\u4ea4\u91cf": ["1100", "1000"],
            "\u6210\u4ea4\u989d": ["11000", "10000"],
        }
    )


def _english_price_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2023-12-29", "2024-01-02", "2024-01-01"],
            "open": ["1.9", "2.1", "2.0"],
            "high": ["2.1", "2.4", "2.3"],
            "low": ["1.8", "2.0", "1.9"],
            "close": ["2.0", "2.3", "2.2"],
            "volume": ["900", "1100", "1000"],
            "amount": ["9000", "11000", "10000"],
        }
    )


def _eastmoney_quote_response(
    symbol: str,
    *,
    price: float = 1.25,
    previous_close: float = 1.24,
    timestamp: int = 1704351000,
) -> FakeResponse:
    return FakeResponse(
        json_data={
            "data": {
                "f43": price,
                "f47": 1200,
                "f48": 15000,
                "f57": symbol,
                "f60": previous_close,
                "f86": timestamp,
            }
        }
    )


def _sina_quote_content(symbol: str) -> bytes:
    values = [""] * 32
    values[0] = symbol
    values[1] = "1.20"
    values[2] = "1.24"
    values[3] = "1.25"
    values[4] = "1.30"
    values[5] = "1.19"
    values[8] = "1200"
    values[9] = "15000"
    values[30] = "2024-01-04"
    values[31] = "14:50:00"
    return f'var hq_str_{_sina_exchange_symbol(symbol)}="{",".join(values)}";'.encode(
        "gb18030"
    )


def _sina_exchange_symbol(symbol: str) -> str:
    return f"sh{symbol}" if symbol.startswith("5") else f"sz{symbol}"
