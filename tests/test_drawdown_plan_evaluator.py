from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest

from fund_alert_bot.market_data import AssetType, RealtimeQuote
from fund_alert_bot.rules.drawdown_plan import (
    ActiveDrawdownCycle,
    DrawdownPlanConfig,
    DrawdownTier,
    _tier_command_text,
    build_drawdown_plan_alert,
    build_drawdown_plan_pre_alert,
    evaluate_drawdown_plan,
    evaluate_drawdown_plan_realtime,
    parse_drawdown_plan_config,
    required_history_start,
    validate_realtime_quote,
)


def test_plan_config_applies_defaults_and_preserves_incremental_amounts() -> None:
    config = parse_drawdown_plan_config(
        reference_symbol="510300",
        asset_type=AssetType.CN_ETF,
        params={
            "investment_fund_symbol": "000001",
            "tiers": [
                {"drawdown": 0.15, "amount": 5000},
                {"drawdown": 0.20, "amount": 10000.5},
            ],
        },
    )

    assert config.lookback_days == 365
    assert config.sma_window == 250
    assert config.sma_slope_window == 20
    assert config.tiers == (
        DrawdownTier(0.15, 5000, "0.15"),
        DrawdownTier(0.20, 10000.5, "0.2"),
    )


def test_mark_added_fallback_keeps_canonical_tier_precision() -> None:
    tier = DrawdownTier(0.123456789, 100.25, "0.123456789")

    assert _tier_command_text((tier,)) == "12.3456789"


@pytest.mark.parametrize(
    ("reference_symbol", "fund_symbol"),
    [("５１０３００", "000001"), ("510300", "０００００１")],
)
def test_plan_config_requires_ascii_symbols(
    reference_symbol: str,
    fund_symbol: str,
) -> None:
    with pytest.raises(ValueError, match="six digits"):
        parse_drawdown_plan_config(
            reference_symbol=reference_symbol,
            asset_type=AssetType.CN_ETF,
            params={
                "investment_fund_symbol": fund_symbol,
                "tiers": [{"drawdown": 0.15, "amount": 5000}],
            },
        )


def test_required_history_range_covers_trend_and_locked_peak() -> None:
    config = _config([(0.15, 5000)], lookback_days=365)

    trend_start = required_history_start(
        evaluation_date=date(2024, 12, 31),
        config=config,
    )
    locked_peak_start = required_history_start(
        evaluation_date=date(2024, 12, 31),
        config=config,
        active_peak_date=date(2022, 6, 1),
    )

    assert trend_start == date(2023, 7, 11)
    assert locked_peak_start == date(2022, 6, 1)


@pytest.mark.parametrize(
    ("params_update", "message"),
    [
        ({"lookback_days": 0}, "lookback_days"),
        ({"sma_window": 1}, "sma_window"),
        ({"sma_slope_window": 0}, "sma_slope_window"),
        ({"tiers": []}, "tiers"),
        ({"tiers": [{"drawdown": 0, "amount": 1}]}, "drawdown"),
        ({"tiers": [{"drawdown": 1, "amount": 1}]}, "drawdown"),
        ({"tiers": [{"drawdown": 0.1, "amount": 0}]}, "amount"),
        (
            {
                "tiers": [
                    {"drawdown": 0.1, "amount": 1e308},
                    {"drawdown": 0.2, "amount": 1e308},
                ]
            },
            "finite total",
        ),
        (
            {
                "tiers": [
                    {"drawdown": 0.2, "amount": 1},
                    {"drawdown": 0.1, "amount": 1},
                ]
            },
            "strictly ascending",
        ),
        (
            {
                "tiers": [
                    {"drawdown": 0.1, "amount": 1},
                    {"drawdown": 0.1, "amount": 2},
                ]
            },
            "strictly ascending",
        ),
    ],
)
def test_plan_config_rejects_invalid_parameters(
    params_update: dict[str, Any], message: str
) -> None:
    params = {
        "investment_fund_symbol": "000001",
        "tiers": [{"drawdown": 0.15, "amount": 5000}],
        **params_update,
    }

    with pytest.raises(ValueError, match=message):
        parse_drawdown_plan_config(
            reference_symbol="510300",
            asset_type=AssetType.CN_ETF,
            params=params,
        )


def test_exact_threshold_triggers_but_price_slightly_above_does_not() -> None:
    config = _config([(0.15, 5000)])

    exact = evaluate_drawdown_plan(
        _history([100, 85]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 2),
    )
    above = evaluate_drawdown_plan(
        _history([100, 85.01]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 2),
    )

    assert [tier.key for tier in exact.newly_crossed_tiers] == ["0.15"]
    assert above.newly_crossed_tiers == ()


def test_gap_crossing_aggregates_only_new_open_tiers() -> None:
    evaluation = evaluate_drawdown_plan(
        _history([100, 74]),
        _config([(0.15, 5000), (0.20, 10000), (0.25, 15000), (0.30, 20000)]),
        reference_symbol="510300",
        expected_date=date(2024, 1, 2),
    )

    assert [tier.key for tier in evaluation.newly_crossed_tiers] == [
        "0.15",
        "0.2",
        "0.25",
    ]
    assert evaluation.total_amount == 30000


def test_recorded_tier_does_not_repeat_after_recovery_without_new_peak() -> None:
    history = _history([100, 84, 95, 84])
    evaluation = evaluate_drawdown_plan(
        history,
        _config([(0.15, 5000), (0.20, 10000)]),
        reference_symbol="510300",
        expected_date=date(2024, 1, 4),
        active_cycle=ActiveDrawdownCycle(
            cycle_id=1,
            peak_date=date(2024, 1, 1),
            peak_price=100,
            last_evaluated_date=date(2024, 1, 2),
        ),
        recorded_tier_keys={"0.15"},
    )

    assert evaluation.cycle_changed is False
    assert evaluation.newly_crossed_tiers == ()


def test_deeper_then_partial_recovery_never_reverses_or_repeats_tiers() -> None:
    config = _config([(0.15, 5000), (0.20, 10000), (0.25, 15000), (0.30, 20000)])
    active = ActiveDrawdownCycle(1, date(2024, 1, 1), 100, date(2024, 1, 2))
    day_two = evaluate_drawdown_plan(
        _history([100, 85, 70]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 3),
        active_cycle=active,
        recorded_tier_keys={"0.15"},
    )
    day_three = evaluate_drawdown_plan(
        _history([100, 85, 70, 80]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 4),
        active_cycle=ActiveDrawdownCycle(
            1,
            date(2024, 1, 1),
            100,
            date(2024, 1, 3),
        ),
        recorded_tier_keys={"0.15", "0.2", "0.25", "0.3"},
    )

    assert [tier.key for tier in day_two.newly_crossed_tiers] == [
        "0.2",
        "0.25",
        "0.3",
    ]
    assert day_two.total_amount == 45000
    assert day_three.drawdown == pytest.approx(0.20)
    assert day_three.newly_crossed_tiers == ()


def test_new_high_starts_cycle_and_rearms_tiers() -> None:
    evaluation = evaluate_drawdown_plan(
        _history([100, 84, 101, 85.85]),
        _config([(0.15, 5000)]),
        reference_symbol="510300",
        expected_date=date(2024, 1, 4),
        active_cycle=ActiveDrawdownCycle(
            1,
            date(2024, 1, 1),
            100,
            date(2024, 1, 2),
        ),
        recorded_tier_keys={"0.15"},
    )

    assert evaluation.cycle_changed is True
    assert evaluation.peak_date == date(2024, 1, 3)
    assert evaluation.peak_price == 101
    assert [tier.key for tier in evaluation.newly_crossed_tiers] == ["0.15"]


def test_equal_peak_after_decline_starts_cycle_but_repeated_equal_does_not() -> None:
    active = ActiveDrawdownCycle(1, date(2024, 1, 1), 100, date(2024, 1, 2))
    recovered = evaluate_drawdown_plan(
        _history([100, 84, 100, 85]),
        _config([(0.15, 5000)]),
        reference_symbol="510300",
        expected_date=date(2024, 1, 4),
        active_cycle=active,
        recorded_tier_keys={"0.15"},
    )
    repeated = evaluate_drawdown_plan(
        _history([100, 100, 100]),
        _config([(0.15, 5000)]),
        reference_symbol="510300",
        expected_date=date(2024, 1, 3),
        active_cycle=active,
        recorded_tier_keys={"0.15"},
    )

    assert recovered.cycle_changed is True
    assert recovered.peak_date == date(2024, 1, 3)
    assert [tier.key for tier in recovered.newly_crossed_tiers] == ["0.15"]
    assert repeated.cycle_changed is False


def test_downtime_crossing_that_recovered_is_not_backfilled() -> None:
    evaluation = evaluate_drawdown_plan(
        _history([100, 70, 90]),
        _config([(0.15, 5000), (0.20, 10000), (0.25, 15000)]),
        reference_symbol="510300",
        expected_date=date(2024, 1, 3),
        active_cycle=ActiveDrawdownCycle(
            1,
            date(2024, 1, 1),
            100,
            date(2024, 1, 1),
        ),
    )

    assert evaluation.drawdown == pytest.approx(0.10)
    assert evaluation.newly_crossed_tiers == ()


def test_initial_peak_uses_inclusive_calendar_window_and_latest_equal_high() -> None:
    history = _dated_history(
        ["2023-01-02", "2023-01-03", "2023-06-01", "2024-01-02"],
        [200, 100, 100, 90],
    )
    evaluation = evaluate_drawdown_plan(
        history,
        _config([(0.10, 1000)], lookback_days=365),
        reference_symbol="510300",
        expected_date=date(2024, 1, 2),
    )

    assert evaluation.peak_date == date(2023, 6, 1)
    assert evaluation.peak_price == 100
    assert evaluation.drawdown == pytest.approx(0.10)


def test_initial_peak_does_not_pair_a_nearby_lower_close_with_the_maximum() -> None:
    evaluation = evaluate_drawdown_plan(
        _history([100, 99.99999, 90]),
        _config([(0.10, 1000)]),
        reference_symbol="510300",
        expected_date=date(2024, 1, 3),
    )

    assert evaluation.peak_date == date(2024, 1, 1)
    assert evaluation.peak_price == 100


def test_missing_sma_history_does_not_block_tier_or_alert() -> None:
    config = _config([(0.15, 5000)], sma_window=250, slope_window=20)
    evaluation = evaluate_drawdown_plan(
        _history([100, 85]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 2),
    )
    alert = build_drawdown_plan_alert(
        rule_id=7,
        reference_symbol="510300",
        name="A500",
        config=config,
        evaluation=evaluation,
    )

    assert evaluation.sma is None
    assert evaluation.distance_to_sma is None
    assert evaluation.sma_slope is None
    assert alert is not None
    assert "MA250: unavailable (insufficient history)" in str(alert["message"])
    assert "shorter than 365 calendar days" in str(alert["message"])
    assert alert["payload"]["coverage_start"] == "2024-01-01"


def test_alert_preserves_decimal_tier_percentage() -> None:
    config = _config([(0.155, 5000)])
    evaluation = evaluate_drawdown_plan(
        _history([100, 84.5]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 2),
    )

    alert = build_drawdown_plan_alert(
        rule_id=7,
        reference_symbol="510300",
        name="A500",
        config=config,
        evaluation=evaluation,
    )

    assert alert is not None
    assert "-15.5% → ¥5,000" in str(alert["message"])


def test_alert_aggregates_tiers_and_carries_trend_payload() -> None:
    config = _config(
        [(0.15, 5000), (0.20, 10000)],
        sma_window=2,
        slope_window=1,
    )
    evaluation = evaluate_drawdown_plan(
        _history([100, 90, 79]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 3),
    )

    alert = build_drawdown_plan_alert(
        rule_id=7,
        reference_symbol="510300",
        name="A500",
        config=config,
        evaluation=evaluation,
    )

    assert alert is not None
    assert alert["alert_key"] == "7:drawdown_plan:peak:2024-01-01:tiers:0.15,0.2"
    payload = alert["payload"]
    assert isinstance(payload, dict)
    assert payload["crossed_tiers"] == [
        {"key": "0.15", "drawdown": 0.15, "amount": 5000},
        {"key": "0.2", "drawdown": 0.2, "amount": 10000},
    ]
    assert payload["total_amount"] == 15000
    assert payload["data_date"] == "2024-01-03"
    assert payload["sma"] == pytest.approx(84.5)
    assert "Total additional amount now due: ¥15,000" in str(alert["message"])
    assert "No trade has been placed" in str(alert["message"])


def test_confirmed_history_fails_closed_on_stale_or_wrong_basis() -> None:
    stale = _history([100])
    wrong_basis = _history([100])
    wrong_basis.attrs["price_basis"] = "unadjusted"
    unsupported_source = _history([100])
    unsupported_source.attrs["source"] = "sina_unadjusted"
    unsupported_source["source"] = "sina_unadjusted"

    with pytest.raises(ValueError, match="does not contain closing data"):
        evaluate_drawdown_plan(
            stale,
            _config([(0.15, 5000)]),
            reference_symbol="510300",
            expected_date=date(2024, 1, 2),
        )
    with pytest.raises(ValueError, match="price_basis"):
        evaluate_drawdown_plan(
            wrong_basis,
            _config([(0.15, 5000)]),
            reference_symbol="510300",
            expected_date=date(2024, 1, 1),
        )
    with pytest.raises(ValueError, match="source is unsupported"):
        evaluate_drawdown_plan(
            unsupported_source,
            _config([(0.15, 5000)]),
            reference_symbol="510300",
            expected_date=date(2024, 1, 1),
        )


def test_confirmed_history_rejects_invalid_close_inside_peak_window() -> None:
    with pytest.raises(ValueError, match="invalid closing prices"):
        evaluate_drawdown_plan(
            _history([100, float("nan"), 85]),
            _config([(0.15, 5000)]),
            reference_symbol="510300",
            expected_date=date(2024, 1, 3),
        )


def test_realtime_quote_requires_activity_and_continuity() -> None:
    valid = RealtimeQuote(
        symbol="510300",
        price=85,
        previous_close=90,
        volume=100,
        amount=1000,
        source="sina_fallback",
        fetched_at=datetime(2024, 1, 2, 6, 50, tzinfo=UTC),
    )

    assert (
        validate_realtime_quote(
            valid,
            reference_symbol="510300",
            confirmed_previous_close=90,
        )
        is valid
    )
    with pytest.raises(ValueError, match="no evidence"):
        validate_realtime_quote(
            RealtimeQuote(
                symbol="510300",
                price=85,
                previous_close=90,
                volume=0,
                amount=0,
                source="eastmoney",
                fetched_at=valid.fetched_at,
            ),
            reference_symbol="510300",
            confirmed_previous_close=90,
        )
    with pytest.raises(ValueError, match="does not match"):
        validate_realtime_quote(
            valid,
            reference_symbol="510300",
            confirmed_previous_close=91,
        )
    with pytest.raises(ValueError, match="source is unsupported"):
        validate_realtime_quote(
            RealtimeQuote(
                symbol="510300",
                price=85,
                previous_close=90,
                volume=100,
                amount=1000,
                source="unknown",
                fetched_at=valid.fetched_at,
            ),
            reference_symbol="510300",
            confirmed_previous_close=90,
        )


def test_realtime_plan_crossing_uses_quote_without_consuming_recorded_tiers() -> None:
    config = _config([(0.15, 5000), (0.20, 10000)], sma_window=2)
    confirmed = evaluate_drawdown_plan(
        _history([100]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 1),
    )
    quote = RealtimeQuote(
        symbol="510300",
        price=79,
        previous_close=100,
        volume=100,
        amount=1000,
        source="eastmoney",
        fetched_at=datetime(2024, 1, 2, 6, 50, tzinfo=UTC),
    )

    realtime = evaluate_drawdown_plan_realtime(
        confirmed,
        config,
        quote,
        reference_symbol="510300",
        market_date=date(2024, 1, 2),
        recorded_tier_keys={"0.15"},
    )
    alert = build_drawdown_plan_pre_alert(
        rule_id=7,
        cycle_id=9,
        reference_symbol="510300",
        name="A500",
        confirmed_date=date(2024, 1, 1),
        config=config,
        evaluation=realtime,
        quote=quote,
    )

    assert realtime.drawdown == pytest.approx(0.21)
    assert [tier.key for tier in realtime.newly_crossed_tiers] == ["0.2"]
    assert realtime.total_amount == 10000
    assert alert is not None
    assert alert["alert_key"] == "7:drawdown_plan:pre_alert:2024-01-02"
    assert alert["payload"]["phase"] == "before_close"
    assert alert["payload"]["cycle_id"] == 9
    assert alert["payload"]["confirmed_close_date"] == "2024-01-01"
    assert "Realtime estimate before close" in alert["message"]


def test_realtime_plan_rejects_quote_fetched_on_another_market_date() -> None:
    config = _config([(0.15, 5000)])
    confirmed = evaluate_drawdown_plan(
        _history([100]),
        config,
        reference_symbol="510300",
        expected_date=date(2024, 1, 1),
    )

    with pytest.raises(ValueError, match="not fetched on the market date"):
        evaluate_drawdown_plan_realtime(
            confirmed,
            config,
            RealtimeQuote(
                symbol="510300",
                price=84,
                previous_close=100,
                volume=100,
                amount=1000,
                source="eastmoney",
                fetched_at=datetime(2024, 1, 1, 6, 50, tzinfo=UTC),
            ),
            reference_symbol="510300",
            market_date=date(2024, 1, 2),
        )


def _config(
    tiers: list[tuple[float, int]],
    *,
    lookback_days: int = 365,
    sma_window: int = 250,
    slope_window: int = 20,
) -> DrawdownPlanConfig:
    return parse_drawdown_plan_config(
        reference_symbol="510300",
        asset_type=AssetType.CN_ETF,
        params={
            "investment_fund_symbol": "000001",
            "lookback_days": lookback_days,
            "tiers": [
                {"drawdown": drawdown, "amount": amount} for drawdown, amount in tiers
            ],
            "sma_window": sma_window,
            "sma_slope_window": slope_window,
        },
    )


def _history(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes))
    return _dated_history(
        [timestamp.date().isoformat() for timestamp in dates],
        closes,
    )


def _dated_history(dates: list[str], closes: list[float]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "close": closes,
            "source": ["akshare_eastmoney"] * len(closes),
        }
    )
    frame.attrs.update(
        {
            "symbol": "510300",
            "source": "akshare_eastmoney",
            "price_basis": "qfq",
            "frequency": "daily",
        }
    )
    return frame
