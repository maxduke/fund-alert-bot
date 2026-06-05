"""Market data exception types."""

from __future__ import annotations


class MarketDataProviderError(Exception):
    """Base exception for market data provider errors."""


class UnsupportedAssetTypeError(MarketDataProviderError):
    """Raised when a provider does not support an instrument asset type."""


class EmptyMarketDataError(MarketDataProviderError):
    """Raised when a provider returns no usable market data."""


class MarketDataNormalizeError(MarketDataProviderError):
    """Raised when market data cannot be normalized to the project schema."""


class MarketDataFetchError(MarketDataProviderError):
    """Raised when a provider call fails after retries."""
