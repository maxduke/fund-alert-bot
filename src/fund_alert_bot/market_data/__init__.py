"""Market data providers and normalization helpers."""

from fund_alert_bot.market_data.akshare_provider import AkshareMarketDataProvider
from fund_alert_bot.market_data.exceptions import (
    EmptyMarketDataError,
    MarketDataFetchError,
    MarketDataNormalizeError,
    MarketDataProviderError,
    UnsupportedAssetTypeError,
)
from fund_alert_bot.market_data.models import AssetType, Instrument
from fund_alert_bot.market_data.provider import MarketDataProvider

__all__ = [
    "AkshareMarketDataProvider",
    "AssetType",
    "EmptyMarketDataError",
    "Instrument",
    "MarketDataFetchError",
    "MarketDataNormalizeError",
    "MarketDataProvider",
    "MarketDataProviderError",
    "UnsupportedAssetTypeError",
]
