"""Normalize raw market data into the project schema."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from fund_alert_bot.market_data.exceptions import (
    EmptyMarketDataError,
    MarketDataNormalizeError,
    UnsupportedAssetTypeError,
)
from fund_alert_bot.market_data.models import AssetType

NORMALIZED_COLUMNS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
]
NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
PRICE_INPUT_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
OPEN_FUND_EMPTY_COLUMNS = ["open", "high", "low", "volume", "amount"]

PRICE_COLUMN_MAPPINGS = {
    "\u65e5\u671f": "date",
    "\u5f00\u76d8": "open",
    "\u6700\u9ad8": "high",
    "\u6700\u4f4e": "low",
    "\u6536\u76d8": "close",
    "\u6210\u4ea4\u91cf": "volume",
    "\u6210\u4ea4\u989d": "amount",
}
OPEN_FUND_COLUMN_MAPPINGS = {
    "\u51c0\u503c\u65e5\u671f": "date",
    "\u5355\u4f4d\u51c0\u503c": "close",
}


def normalize_history(
    raw_data: pd.DataFrame,
    asset_type: AssetType,
    *,
    source: str,
) -> pd.DataFrame:
    """Normalize AKShare history into a shared daily price schema."""

    if not isinstance(source, str) or not source.strip():
        raise MarketDataNormalizeError("Market data source must not be empty.")
    source = source.strip()
    if raw_data is None or raw_data.empty:
        raise EmptyMarketDataError("Market data provider returned no rows.")

    resolved_asset_type = _resolve_asset_type(asset_type)
    if resolved_asset_type in {
        AssetType.CN_INDEX,
        AssetType.CN_ETF,
        AssetType.CN_STOCK,
    }:
        return _normalize_price_history(raw_data, source=source)
    if resolved_asset_type is AssetType.CN_OPEN_FUND:
        return _normalize_open_fund_history(raw_data, source=source)

    raise UnsupportedAssetTypeError(f"Unsupported asset type: {asset_type!r}")


def _normalize_price_history(raw_data: pd.DataFrame, *, source: str) -> pd.DataFrame:
    if _has_columns(raw_data, PRICE_INPUT_COLUMNS):
        frame = raw_data.copy()
    else:
        _ensure_columns(raw_data, PRICE_COLUMN_MAPPINGS)
        frame = raw_data.rename(columns=PRICE_COLUMN_MAPPINGS).copy()
    frame["source"] = source
    return _finalize_frame(frame[NORMALIZED_COLUMNS])


def _normalize_open_fund_history(
    raw_data: pd.DataFrame, *, source: str
) -> pd.DataFrame:
    _ensure_columns(raw_data, OPEN_FUND_COLUMN_MAPPINGS)

    frame = raw_data.rename(columns=OPEN_FUND_COLUMN_MAPPINGS).copy()
    for column in OPEN_FUND_EMPTY_COLUMNS:
        frame[column] = None
    frame["source"] = source

    normalized = _finalize_frame(frame[NORMALIZED_COLUMNS], allow_empty_ohlc=True)
    for column in OPEN_FUND_EMPTY_COLUMNS:
        normalized[column] = None
    return normalized


def _finalize_frame(
    frame: pd.DataFrame, *, allow_empty_ohlc: bool = False
) -> pd.DataFrame:
    normalized = frame.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    if normalized["date"].isna().any():
        raise MarketDataNormalizeError("Market data contains invalid dates.")

    present_masks = {column: normalized[column].notna() for column in NUMERIC_COLUMNS}
    for column in NUMERIC_COLUMNS:
        if column in OPEN_FUND_EMPTY_COLUMNS and normalized[column].isna().all():
            continue
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    _validate_numeric_values(
        normalized, present_masks, allow_empty_ohlc=allow_empty_ohlc
    )

    normalized = (
        normalized.sort_values("date", ascending=True, kind="stable")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    if normalized.empty:
        raise EmptyMarketDataError("Market data normalization produced no rows.")
    return normalized[NORMALIZED_COLUMNS]


def _validate_numeric_values(
    frame: pd.DataFrame,
    present_masks: dict[str, pd.Series],
    *,
    allow_empty_ohlc: bool,
) -> None:
    """Reject malformed provider values without dropping their rows."""

    for column in ("open", "high", "low", "close"):
        values = frame[column]
        present = present_masks[column]
        invalid = present & ~values.map(_is_positive_finite)
        if column == "close" or not allow_empty_ohlc:
            invalid |= ~present
        if invalid.any():
            raise MarketDataNormalizeError(
                f"Market data contains invalid {column} values."
            )

    for column in ("volume", "amount"):
        values = frame[column]
        present = present_masks[column]
        invalid = present & ~values.map(_is_non_negative_finite)
        if invalid.any():
            raise MarketDataNormalizeError(
                f"Market data contains invalid {column} values."
            )

    ohlc_present = frame[["open", "high", "low", "close"]].notna().all(axis=1)
    if not ohlc_present.any():
        return

    ohlc = frame.loc[ohlc_present, ["open", "high", "low", "close"]]
    invalid_relationship = (
        (ohlc["high"] < ohlc[["open", "close"]].max(axis=1))
        | (ohlc["low"] > ohlc[["open", "close"]].min(axis=1))
        | (ohlc["high"] < ohlc["low"])
    )
    if invalid_relationship.any():
        raise MarketDataNormalizeError("Market data contains inconsistent OHLC values.")


def _is_positive_finite(value: object) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _is_non_negative_finite(value: object) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric >= 0


def _ensure_columns(raw_data: pd.DataFrame, mappings: dict[str, str]) -> None:
    missing_columns = [column for column in mappings if column not in raw_data.columns]
    if missing_columns:
        joined_columns = ", ".join(missing_columns)
        raise MarketDataNormalizeError(
            f"Market data is missing required columns: {joined_columns}"
        )


def _has_columns(raw_data: pd.DataFrame, columns: list[str]) -> bool:
    return all(column in raw_data.columns for column in columns)


def _resolve_asset_type(asset_type: AssetType | str | Any) -> AssetType:
    try:
        return AssetType(asset_type)
    except ValueError as exc:
        raise UnsupportedAssetTypeError(
            f"Unsupported asset type: {asset_type!r}"
        ) from exc
