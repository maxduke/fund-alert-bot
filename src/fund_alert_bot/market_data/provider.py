"""Market data provider interface."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

import pandas as pd

from fund_alert_bot.market_data.models import Instrument

DateLike = str | date | datetime | pd.Timestamp


class MarketDataProvider(Protocol):
    """Interface for normalized market data providers."""

    def get_history(
        self,
        instrument: Instrument,
        start_date: DateLike,
        end_date: DateLike,
    ) -> pd.DataFrame:
        """Return normalized daily history for an instrument."""

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        """Return the latest normalized row for an instrument."""
