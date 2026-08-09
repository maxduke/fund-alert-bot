"""Market data provider interface."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

import pandas as pd

from fund_alert_bot.market_data.models import (
    FundNav,
    Instrument,
    PriceBasis,
    RealtimeQuote,
)

DateLike = str | date | datetime | pd.Timestamp


class MarketDataProvider(Protocol):
    """Interface for normalized market data providers."""

    def get_history(
        self,
        instrument: Instrument,
        start_date: DateLike,
        end_date: DateLike,
        *,
        price_basis: PriceBasis = PriceBasis.UNADJUSTED,
    ) -> pd.DataFrame:
        """Return normalized daily history for an instrument."""

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        """Return the latest normalized row for an instrument."""

    def get_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        """Return a normalized ETF quote, using the supported fallback."""

    def get_sina_etf_realtime_quote(self, instrument: Instrument) -> RealtimeQuote:
        """Return the normalized Sina fallback ETF quote."""

    def get_fund_nav(
        self,
        instrument: Instrument,
        nav_date: DateLike | None = None,
    ) -> FundNav:
        """Return a validated exact-date or latest feeder-fund unit NAV."""
