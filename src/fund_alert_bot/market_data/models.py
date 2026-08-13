"""Shared market data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class AssetType(StrEnum):
    """Supported market data asset types."""

    CN_INDEX = "cn_index"
    CN_ETF = "cn_etf"
    CN_STOCK = "cn_stock"
    CN_OPEN_FUND = "cn_open_fund"


class PriceBasis(StrEnum):
    """Supported daily-price adjustment bases."""

    UNADJUSTED = "unadjusted"
    QFQ = "qfq"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A market instrument tracked by the reminder bot."""

    symbol: str
    name: str
    asset_type: AssetType


@dataclass(frozen=True, slots=True)
class RealtimeQuote:
    """Normalized ETF realtime quote used for provisional plan checks."""

    symbol: str
    price: float | None
    previous_close: float | None
    volume: float | None
    amount: float | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FundNav:
    """A validated Investment Feeder Fund unit NAV with its published date."""

    symbol: str
    date: date
    value: float
    source: str
