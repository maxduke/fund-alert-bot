"""Market data provider placeholders."""

from __future__ import annotations

from typing import Protocol


class MarketDataProvider(Protocol):
    """Interface for future market data providers."""

    def fetch(self, symbol: str) -> object:
        """Fetch market data for a symbol."""
