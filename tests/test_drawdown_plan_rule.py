from __future__ import annotations

from collections.abc import Callable

import pandas as pd
import pytest

from fund_alert_bot.rules.drawdown_plan import (
    calculate_sma,
    calculate_sma_distance,
    calculate_sma_slope,
)


def test_calculate_sma_normalizes_history() -> None:
    history = pd.DataFrame(
        {
            "date": [
                "2024-01-03",
                "2024-01-01",
                "2024-01-02",
                "2024-01-02",
                "2024-01-04",
                "2024-01-05",
                "2024-01-06",
            ],
            "close": [3, 1, 2, 20, float("nan"), float("inf"), 0],
        }
    )

    assert calculate_sma(history, window=3) == pytest.approx(8)


@pytest.mark.parametrize(
    ("current_price", "expected"),
    [(110, 0.1), (90, -0.1), (100, 0)],
)
def test_calculate_sma_distance(current_price: float, expected: float) -> None:
    assert calculate_sma_distance(current_price, 100) == pytest.approx(expected)


def test_sma_values_are_unavailable_with_insufficient_history() -> None:
    history = _history([1, 2, 3, 4])

    assert calculate_sma(history, window=5) is None
    assert calculate_sma_distance(4, None) is None
    assert calculate_sma_slope(history, sma_window=3, slope_window=2) is None


@pytest.mark.parametrize(
    ("closes", "expected"),
    [([1, 2, 3, 4, 5], 1.0), ([5, 4, 3, 2, 1], -0.5)],
)
def test_calculate_sma_slope(closes: list[float], expected: float) -> None:
    assert calculate_sma_slope(
        _history(closes), sma_window=3, slope_window=2
    ) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: calculate_sma(_history([1, 2]), 1), "window"),
        (lambda: calculate_sma_slope(_history([1, 2]), 2, 0), "slope_window"),
        (lambda: calculate_sma_distance(0, 1), "current_price"),
        (lambda: calculate_sma_distance(1, float("nan")), "sma"),
    ],
)
def test_trend_calculation_rejects_invalid_inputs(
    call: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        call()


def test_calculate_sma_rejects_invalid_dates() -> None:
    history = pd.DataFrame({"date": ["not-a-date"], "close": [1]})

    with pytest.raises(ValueError, match="invalid dates"):
        calculate_sma(history, window=2)


def _history(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(closes)),
            "close": closes,
        }
    )
