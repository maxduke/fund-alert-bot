"""Trend calculations for drawdown buy plans."""

from __future__ import annotations

import math

import pandas as pd


def calculate_sma(
    history: pd.DataFrame,
    window: int,
    price_field: str = "close",
) -> float | None:
    """Return the latest simple moving average, or ``None`` if history is short."""

    _validate_window(window, "window", minimum=2)
    closes = _clean_closes(history, price_field)
    if len(closes) < window:
        return None
    return float(closes.iloc[-window:].mean())


def calculate_sma_distance(current_price: float, sma: float | None) -> float | None:
    """Return the percentage distance from price to SMA."""

    if sma is None:
        return None
    if not math.isfinite(current_price) or current_price <= 0:
        raise ValueError("current_price must be a positive finite number.")
    if not math.isfinite(sma) or sma <= 0:
        raise ValueError("sma must be a positive finite number.")
    return current_price / sma - 1


def calculate_sma_slope(
    history: pd.DataFrame,
    sma_window: int,
    slope_window: int,
    price_field: str = "close",
) -> float | None:
    """Return the SMA change over the requested number of trading observations."""

    _validate_window(sma_window, "sma_window", minimum=2)
    _validate_window(slope_window, "slope_window", minimum=1)
    closes = _clean_closes(history, price_field)
    required = sma_window + slope_window
    if len(closes) < required:
        return None

    current_sma = float(closes.iloc[-sma_window:].mean())
    previous_sma = float(closes.iloc[-required:-slope_window].mean())
    return current_sma / previous_sma - 1


def _clean_closes(history: pd.DataFrame, price_field: str) -> pd.Series:
    if "date" not in history.columns:
        raise ValueError("Market data is missing date field.")
    if price_field not in history.columns:
        raise ValueError(f"Market data is missing price field: {price_field}")

    frame = history[["date", price_field]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise ValueError("Market data contains invalid dates.")

    frame = frame.sort_values("date", kind="stable").drop_duplicates(
        "date", keep="last"
    )
    frame[price_field] = pd.to_numeric(frame[price_field], errors="coerce")
    valid = frame[price_field].map(
        lambda value: pd.notna(value) and math.isfinite(value) and value > 0
    )
    return frame.loc[valid, price_field].astype(float).reset_index(drop=True)


def _validate_window(value: int, name: str, *, minimum: int) -> None:
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
