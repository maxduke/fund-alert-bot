"""Shared market data models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AssetType(StrEnum):
    """Supported market data asset types."""

    CN_ETF = "cn_etf"
    CN_OPEN_FUND = "cn_open_fund"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A market instrument tracked by the reminder bot."""

    symbol: str
    name: str
    asset_type: AssetType
