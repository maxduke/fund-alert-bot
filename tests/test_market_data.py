from __future__ import annotations

from datetime import date
from typing import Any, cast

import pandas as pd
import pytest

from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    AssetType,
    EmptyMarketDataError,
    Instrument,
    MarketDataNormalizeError,
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

    def stock_zh_index_daily_em(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("stock_zh_index_daily_em", kwargs))
        return _english_price_history()

    def stock_zh_a_hist(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("stock_zh_a_hist", kwargs))
        return _price_history()

    def fund_etf_hist_sina(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("fund_etf_hist_sina", kwargs))
        return _english_price_history()


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
    assert history["source"].tolist() == ["akshare", "akshare"]
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


def test_get_latest_prefers_realtime_spot_data_for_etf() -> None:
    class RealtimeAkshare(FakeAkshare):
        def fund_etf_spot_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_spot_em", kwargs))
            return pd.DataFrame(
                {
                    "代码": ["510300"],
                    "最新价": ["1.25"],
                    "今开": ["1.20"],
                    "最高": ["1.30"],
                    "最低": ["1.19"],
                    "成交量": ["1200"],
                    "成交额": ["15000"],
                }
            )

    fake_ak = RealtimeAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retry_delay_seconds=0,
        today_factory=lambda: date(2024, 1, 4),
    )
    instrument = Instrument(
        symbol="510300",
        name="CSI 300 ETF",
        asset_type=AssetType.CN_ETF,
    )

    latest = provider.get_latest(instrument)

    assert latest == {
        "date": pd.Timestamp("2024-01-04"),
        "open": 1.2,
        "high": 1.3,
        "low": 1.19,
        "close": 1.25,
        "volume": 1200.0,
        "amount": 15000.0,
        "source": "akshare_realtime",
    }
    assert fake_ak.calls == [("fund_etf_spot_em", {})]


def test_get_latest_reuses_realtime_spot_data_within_ttl() -> None:
    class RealtimeAkshare(FakeAkshare):
        def fund_etf_spot_em(self, **kwargs: Any) -> pd.DataFrame:
            self.calls.append(("fund_etf_spot_em", kwargs))
            return pd.DataFrame(
                {
                    "代码": ["510300", "159915"],
                    "最新价": ["1.25", "2.35"],
                    "今开": ["1.20", "2.30"],
                    "最高": ["1.30", "2.40"],
                    "最低": ["1.19", "2.29"],
                    "成交量": ["1200", "2300"],
                    "成交额": ["15000", "54000"],
                }
            )

    fake_ak = RealtimeAkshare()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retry_delay_seconds=0,
        today_factory=lambda: date(2024, 1, 4),
    )

    first = provider.get_latest(
        Instrument("510300", "CSI 300 ETF", AssetType.CN_ETF)
    )
    second = provider.get_latest(
        Instrument("159915", "ChiNext ETF", AssetType.CN_ETF)
    )

    assert first is not None
    assert first["close"] == 1.25
    assert second is not None
    assert second["close"] == 2.35
    assert fake_ak.calls == [("fund_etf_spot_em", {})]


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
        "source": "akshare",
    }


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
