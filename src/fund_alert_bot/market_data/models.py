"""Shared market data models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AssetType(StrEnum):
    """Supported market data asset types."""

    CN_INDEX = "cn_index"
    CN_ETF = "cn_etf"
    CN_STOCK = "cn_stock"
    CN_OPEN_FUND = "cn_open_fund"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A market instrument tracked by the reminder bot."""

    symbol: str
    name: str
    asset_type: AssetType
