"""Trend calculations for drawdown buy plans."""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from fund_alert_bot.market_data.models import AssetType, RealtimeQuote

_THRESHOLD_TOLERANCE = 1e-12
_PRICE_RELATIVE_TOLERANCE = 1e-4
_PRICE_ABSOLUTE_TOLERANCE = 1e-6
_SYMBOL_PATTERN = re.compile(r"[0-9]{6}")
_REALTIME_SOURCES = frozenset({"eastmoney", "sina_fallback"})
_CONFIRMED_HISTORY_SOURCES = frozenset({"akshare_eastmoney"})


@dataclass(frozen=True, slots=True)
class DrawdownTier:
    """One incremental drawdown-plan amount."""

    drawdown: float
    amount: int | float
    key: str


@dataclass(frozen=True, slots=True)
class DrawdownPlanConfig:
    """Validated drawdown-plan configuration."""

    investment_fund_symbol: str
    lookback_days: int
    tiers: tuple[DrawdownTier, ...]
    sma_window: int
    sma_slope_window: int


@dataclass(frozen=True, slots=True)
class ActiveDrawdownCycle:
    """Persisted active cycle state required by the pure evaluator."""

    cycle_id: int
    peak_date: date
    peak_price: float
    last_evaluated_date: date


@dataclass(frozen=True, slots=True)
class DrawdownPlanEvaluation:
    """Current confirmed-close plan state and newly reached open tiers."""

    latest_date: date
    latest_price: float
    peak_date: date
    peak_price: float
    drawdown: float
    sma: float | None
    above_sma: bool | None
    distance_to_sma: float | None
    sma_slope: float | None
    source: str
    coverage_start: date
    cycle_initialized: bool
    cycle_changed: bool
    newly_crossed_tiers: tuple[DrawdownTier, ...]
    total_amount: int | float


def required_history_start(
    *,
    evaluation_date: date,
    config: DrawdownPlanConfig,
    active_peak_date: date | None = None,
) -> date:
    """Return a safe calendar start for drawdown, trend, and cycle recovery."""

    calendar_days = max(
        config.lookback_days,
        2 * (config.sma_window + config.sma_slope_window),
    )
    start = evaluation_date - timedelta(days=calendar_days - 1)
    if active_peak_date is not None:
        start = min(start, active_peak_date)
    return start


def parse_drawdown_plan_config(
    *,
    reference_symbol: str,
    asset_type: AssetType | str,
    params: Mapping[str, Any],
) -> DrawdownPlanConfig:
    """Validate persisted plan parameters at the evaluator boundary."""

    if AssetType(asset_type) is not AssetType.CN_ETF:
        raise ValueError("drawdown_plan reference asset_type must be cn_etf.")
    if _SYMBOL_PATTERN.fullmatch(reference_symbol) is None:
        raise ValueError("reference ETF symbol must contain exactly six digits.")

    fund_symbol = str(params.get("investment_fund_symbol", ""))
    if _SYMBOL_PATTERN.fullmatch(fund_symbol) is None:
        raise ValueError("investment fund symbol must contain exactly six digits.")
    if fund_symbol == reference_symbol:
        raise ValueError("reference ETF and investment fund symbols must differ.")

    lookback_days = _read_integer(params, "lookback_days", default=365, minimum=1)
    sma_window = _read_integer(params, "sma_window", default=250, minimum=2)
    slope_window = _read_integer(
        params,
        "sma_slope_window",
        default=20,
        minimum=1,
    )
    tiers = _read_tiers(params.get("tiers"))
    return DrawdownPlanConfig(
        investment_fund_symbol=fund_symbol,
        lookback_days=lookback_days,
        tiers=tiers,
        sma_window=sma_window,
        sma_slope_window=slope_window,
    )


def evaluate_drawdown_plan(
    history: pd.DataFrame,
    config: DrawdownPlanConfig,
    *,
    reference_symbol: str,
    expected_date: date,
    active_cycle: ActiveDrawdownCycle | None = None,
    recorded_tier_keys: Collection[str] = (),
) -> DrawdownPlanEvaluation:
    """Evaluate one plan from normalized confirmed closing history."""

    frame = validate_confirmed_plan_history(
        history,
        reference_symbol=reference_symbol,
        expected_date=expected_date,
    )
    latest = frame.iloc[-1]
    latest_date = latest["date"].date()
    latest_price = float(latest["close"])

    if active_cycle is None:
        peak_date, peak_price = _initial_peak(
            frame,
            latest_date=latest_date,
            lookback_days=config.lookback_days,
        )
        cycle_initialized = True
        cycle_changed = False
    else:
        if latest_date < active_cycle.last_evaluated_date:
            raise ValueError("Confirmed history is older than the active cycle state.")
        peak_date, peak_price, cycle_changed = _recover_cycle(frame, active_cycle)
        cycle_initialized = False

    drawdown = max(0.0, 1 - latest_price / peak_price)
    sma = calculate_sma(frame, config.sma_window)
    distance = calculate_sma_distance(latest_price, sma)
    slope = calculate_sma_slope(
        frame,
        config.sma_window,
        config.sma_slope_window,
    )
    already_recorded = set() if cycle_changed else set(recorded_tier_keys)
    crossed = tuple(
        tier
        for tier in config.tiers
        if drawdown + _THRESHOLD_TOLERANCE >= tier.drawdown
        and tier.key not in already_recorded
    )
    total_amount = sum((tier.amount for tier in crossed), start=0)

    return DrawdownPlanEvaluation(
        latest_date=latest_date,
        latest_price=latest_price,
        peak_date=peak_date,
        peak_price=peak_price,
        drawdown=drawdown,
        sma=sma,
        above_sma=None if sma is None else latest_price > sma,
        distance_to_sma=distance,
        sma_slope=slope,
        source=str(history.attrs["source"]),
        coverage_start=frame.iloc[0]["date"].date(),
        cycle_initialized=cycle_initialized,
        cycle_changed=cycle_changed,
        newly_crossed_tiers=crossed,
        total_amount=total_amount,
    )


def validate_confirmed_plan_history(
    history: pd.DataFrame,
    *,
    reference_symbol: str,
    expected_date: date,
) -> pd.DataFrame:
    """Fail closed unless normalized history has the requested identity and basis."""

    expected_metadata = {
        "symbol": reference_symbol,
        "price_basis": "qfq",
        "frequency": "daily",
    }
    for key, expected in expected_metadata.items():
        if history.attrs.get(key) != expected:
            raise ValueError(f"Confirmed history has invalid {key} metadata.")
    source = history.attrs.get("source")
    if not isinstance(source, str) or source not in _CONFIRMED_HISTORY_SOURCES:
        raise ValueError("Confirmed history source is unsupported.")

    frame = _clean_history(history, "close", reject_invalid_prices=True)
    if frame.empty:
        raise ValueError("Confirmed history has no valid closing prices.")
    if frame.iloc[-1]["date"].date() != expected_date:
        raise ValueError(
            f"Confirmed history does not contain closing data for {expected_date}."
        )
    if "source" in frame.columns:
        if frame["source"].isna().any() or set(frame["source"]) != {source}:
            raise ValueError("Confirmed history contains inconsistent sources.")
    return frame


def validate_realtime_quote(
    quote: RealtimeQuote,
    *,
    reference_symbol: str,
    confirmed_previous_close: float,
) -> RealtimeQuote:
    """Validate an ETF quote for a provisional before-close reminder."""

    if quote.symbol != reference_symbol:
        raise ValueError("Realtime quote symbol does not match the plan reference ETF.")
    if quote.source not in _REALTIME_SOURCES:
        raise ValueError("Realtime quote source is unsupported.")
    if quote.fetched_at.tzinfo is None or quote.fetched_at.utcoffset() is None:
        raise ValueError("Realtime quote fetched_at must be timezone-aware.")
    _require_positive_finite(quote.price, "realtime price")
    _require_positive_finite(quote.previous_close, "realtime previous close")
    _require_positive_finite(confirmed_previous_close, "confirmed previous close")
    if not _has_positive_activity(quote.volume, quote.amount):
        raise ValueError("Realtime quote has no evidence of current-session trading.")
    if not math.isclose(
        float(quote.previous_close),
        confirmed_previous_close,
        rel_tol=_PRICE_RELATIVE_TOLERANCE,
        abs_tol=_PRICE_ABSOLUTE_TOLERANCE,
    ):
        raise ValueError("Realtime previous close does not match confirmed history.")
    return quote


def evaluate_drawdown_plan_realtime(
    confirmed: DrawdownPlanEvaluation,
    config: DrawdownPlanConfig,
    quote: RealtimeQuote,
    *,
    reference_symbol: str,
    market_date: date,
    recorded_tier_keys: Collection[str] = (),
) -> DrawdownPlanEvaluation:
    """Apply one validated current-session quote to confirmed plan context."""

    validate_realtime_quote(
        quote,
        reference_symbol=reference_symbol,
        confirmed_previous_close=confirmed.latest_price,
    )
    if confirmed.latest_date >= market_date:
        raise ValueError("Realtime estimate requires an earlier confirmed close.")
    if quote.fetched_at.astimezone(ZoneInfo("Asia/Shanghai")).date() != market_date:
        raise ValueError("Realtime quote was not fetched on the market date.")

    price = float(quote.price)
    drawdown = max(0.0, 1 - price / confirmed.peak_price)
    already_recorded = set() if confirmed.cycle_changed else set(recorded_tier_keys)
    crossed = tuple(
        tier
        for tier in config.tiers
        if drawdown + _THRESHOLD_TOLERANCE >= tier.drawdown
        and tier.key not in already_recorded
    )
    return replace(
        confirmed,
        latest_date=market_date,
        latest_price=price,
        drawdown=drawdown,
        above_sma=None if confirmed.sma is None else price > confirmed.sma,
        distance_to_sma=calculate_sma_distance(price, confirmed.sma),
        source=quote.source,
        newly_crossed_tiers=crossed,
        total_amount=sum((tier.amount for tier in crossed), start=0),
    )


def build_drawdown_plan_pre_alert(
    *,
    rule_id: int,
    cycle_id: int,
    reference_symbol: str,
    name: str,
    confirmed_date: date,
    config: DrawdownPlanConfig,
    evaluation: DrawdownPlanEvaluation,
    quote: RealtimeQuote,
) -> dict[str, object] | None:
    """Build one provisional before-close reminder without consuming tiers."""

    tiers = evaluation.newly_crossed_tiers
    if not tiers:
        return None
    tier_lines = [
        f"-{_format_percent(tier.drawdown)} → {_format_amount(tier.amount)}"
        for tier in tiers
    ]
    fetched_at = quote.fetched_at.astimezone(ZoneInfo("Asia/Shanghai"))
    message = "\n".join(
        (
            f"⚠️ Buy-plan pre-alert — {name}",
            "",
            "Realtime estimate before close",
            f"Market date: {evaluation.latest_date.isoformat()}",
            f"Reference ETF: {reference_symbol}",
            f"Investment fund: {config.investment_fund_symbol}",
            f"Realtime drawdown: -{evaluation.drawdown:.1%}",
            f"Recent confirmed peak: {evaluation.peak_price:.6g}",
            f"Realtime price: {evaluation.latest_price:.6g}",
            f"Peak date: {evaluation.peak_date.isoformat()}",
            f"Quote source: {evaluation.source}",
            f"Fetched at: {fetched_at.isoformat(timespec='seconds')}",
            "",
            "🎯 Tiers currently reached:",
            *tier_lines,
            "",
            f"Configured additional amount: {_format_amount(evaluation.total_amount)}",
            "",
            "📈 Long-term trend from confirmed closes",
            *_format_trend(config, evaluation),
            "",
            "Final confirmation will use closing data.",
            "Only after you actually subscribe, record it with:",
            f"/mark_added {rule_id} {_tier_command_text(tiers)}",
            "If you add today, remember to sync the fund position after units settle.",
            "This is a reminder only. No trade has been placed.",
        )
    )
    return {
        "alert_key": (
            f"{rule_id}:drawdown_plan:pre_alert:{evaluation.latest_date.isoformat()}"
        ),
        "title": f"Buy-plan pre-alert — {name}",
        "message": message,
        "payload": {
            "phase": "before_close",
            "rule_id": rule_id,
            "cycle_id": cycle_id,
            "reference_symbol": reference_symbol,
            "investment_fund_symbol": config.investment_fund_symbol,
            "data_date": evaluation.latest_date.isoformat(),
            "confirmed_close_date": confirmed_date.isoformat(),
            "source": evaluation.source,
            "fetched_at": fetched_at.isoformat(),
            "peak_date": evaluation.peak_date.isoformat(),
            "peak_price": evaluation.peak_price,
            "latest_price": evaluation.latest_price,
            "drawdown": evaluation.drawdown,
            "crossed_tiers": [
                {
                    "key": tier.key,
                    "drawdown": tier.drawdown,
                    "amount": tier.amount,
                }
                for tier in tiers
            ],
            "total_amount": evaluation.total_amount,
            "sma_window": config.sma_window,
            "sma": evaluation.sma,
            "above_sma": evaluation.above_sma,
            "distance_to_sma": evaluation.distance_to_sma,
            "sma_slope_window": config.sma_slope_window,
            "sma_slope": evaluation.sma_slope,
        },
    }


def build_drawdown_plan_alert(
    *,
    rule_id: int,
    reference_symbol: str,
    name: str,
    config: DrawdownPlanConfig,
    evaluation: DrawdownPlanEvaluation,
) -> dict[str, object] | None:
    """Build one aggregate reminder for newly reached tiers."""

    tiers = evaluation.newly_crossed_tiers
    if not tiers:
        return None

    tier_lines = [
        f"-{_format_percent(tier.drawdown)} → {_format_amount(tier.amount)}"
        for tier in tiers
    ]
    trend_lines = _format_trend(config, evaluation)
    expected_coverage_start = evaluation.latest_date - timedelta(
        days=config.lookback_days - 1
    )
    coverage_lines: tuple[str, ...] = ()
    if evaluation.coverage_start > expected_coverage_start:
        coverage_lines = (
            "Drawdown history: available since "
            f"{evaluation.coverage_start.isoformat()} "
            f"(shorter than {config.lookback_days} calendar days)",
        )
    message = "\n".join(
        (
            f"📉 Buy-plan reminder — {name}",
            "",
            f"Data date: {evaluation.latest_date.isoformat()}",
            f"Reference ETF: {reference_symbol}",
            f"Investment fund: {config.investment_fund_symbol}",
            f"Current drawdown: -{evaluation.drawdown:.1%}",
            f"Recent peak: {evaluation.peak_price:.6g}",
            f"Current: {evaluation.latest_price:.6g}",
            f"Peak date: {evaluation.peak_date.isoformat()}",
            f"Source: {evaluation.source} (qfq close)",
            *coverage_lines,
            "",
            "🎯 Newly triggered tier" + ("s:" if len(tiers) > 1 else ":"),
            *tier_lines,
            "",
            "Total additional amount now due: "
            f"{_format_amount(evaluation.total_amount)}",
            "",
            "📈 Long-term trend",
            *trend_lines,
            "",
            "Regular DCA continues separately.",
            "Only after you actually subscribe, record it with:",
            f"/mark_added {rule_id} {_tier_command_text(tiers)}",
            "This is a reminder only. No trade has been placed.",
        )
    )
    tier_keys = ",".join(tier.key for tier in tiers)
    return {
        "alert_key": (
            f"{rule_id}:drawdown_plan:peak:{evaluation.peak_date.isoformat()}:"
            f"tiers:{tier_keys}"
        ),
        "title": f"Buy-plan reminder — {name}",
        "message": message,
        "payload": {
            "phase": "after_close",
            "rule_id": rule_id,
            "reference_symbol": reference_symbol,
            "investment_fund_symbol": config.investment_fund_symbol,
            "data_date": evaluation.latest_date.isoformat(),
            "source": evaluation.source,
            "price_basis": "qfq",
            "peak_date": evaluation.peak_date.isoformat(),
            "peak_price": evaluation.peak_price,
            "latest_price": evaluation.latest_price,
            "drawdown": evaluation.drawdown,
            "coverage_start": evaluation.coverage_start.isoformat(),
            "crossed_tiers": [
                {
                    "key": tier.key,
                    "drawdown": tier.drawdown,
                    "amount": tier.amount,
                }
                for tier in tiers
            ],
            "total_amount": evaluation.total_amount,
            "sma_window": config.sma_window,
            "sma": evaluation.sma,
            "above_sma": evaluation.above_sma,
            "distance_to_sma": evaluation.distance_to_sma,
            "sma_slope_window": config.sma_slope_window,
            "sma_slope": evaluation.sma_slope,
        },
    }


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
    return _clean_history(history, price_field)[price_field]


def _clean_history(
    history: pd.DataFrame,
    price_field: str,
    *,
    reject_invalid_prices: bool = False,
) -> pd.DataFrame:
    if "date" not in history.columns:
        raise ValueError("Market data is missing date field.")
    if price_field not in history.columns:
        raise ValueError(f"Market data is missing price field: {price_field}")

    columns = ["date", price_field]
    if "source" in history.columns:
        columns.append("source")
    frame = history[columns].copy()
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
    if reject_invalid_prices and not valid.all():
        raise ValueError("Confirmed history contains invalid closing prices.")
    frame = frame.loc[valid].copy()
    frame[price_field] = frame[price_field].astype(float)
    return frame.reset_index(drop=True)


def _validate_window(value: int, name: str, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}.")


def _read_integer(
    params: Mapping[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{key} must be an integer of at least {minimum}.")
    return value


def _read_tiers(raw_tiers: object) -> tuple[DrawdownTier, ...]:
    if (
        isinstance(raw_tiers, str)
        or not isinstance(raw_tiers, Sequence)
        or not raw_tiers
    ):
        raise ValueError("tiers must be a non-empty sequence.")

    tiers: list[DrawdownTier] = []
    previous_drawdown = 0.0
    for raw_tier in raw_tiers:
        if not isinstance(raw_tier, Mapping):
            raise ValueError("each tier must be an object.")
        drawdown = _read_finite_number(raw_tier.get("drawdown"), "tier drawdown")
        amount = _read_finite_number(raw_tier.get("amount"), "tier amount")
        if drawdown <= 0 or drawdown >= 1:
            raise ValueError("tier drawdown must be greater than 0 and less than 1.")
        if amount <= 0:
            raise ValueError("tier amount must be positive.")
        if drawdown <= previous_drawdown:
            raise ValueError("tiers must be unique and strictly ascending.")
        previous_drawdown = drawdown
        tiers.append(
            DrawdownTier(
                drawdown=drawdown,
                amount=int(amount) if amount.is_integer() else amount,
                key=_canonical_number(drawdown),
            )
        )
    if not math.isfinite(sum(float(tier.amount) for tier in tiers)):
        raise ValueError("tier amounts must have a finite total.")
    return tuple(tiers)


def _read_finite_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number.")
    return number


def _canonical_number(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _initial_peak(
    frame: pd.DataFrame,
    *,
    latest_date: date,
    lookback_days: int,
) -> tuple[date, float]:
    start = pd.Timestamp(latest_date) - pd.Timedelta(days=lookback_days - 1)
    window = frame.loc[frame["date"].between(start, pd.Timestamp(latest_date))]
    if window.empty:
        raise ValueError("Confirmed history has no prices in the lookback window.")
    peak_price = float(window["close"].max())
    peak_rows = window.loc[window["close"] == peak_price]
    return peak_rows.iloc[-1]["date"].date(), peak_price


def _recover_cycle(
    frame: pd.DataFrame,
    active_cycle: ActiveDrawdownCycle,
) -> tuple[date, float, bool]:
    _require_positive_finite(active_cycle.peak_price, "active cycle peak price")
    peak_timestamp = pd.Timestamp(active_cycle.peak_date)
    peak_rows = frame.loc[frame["date"] == peak_timestamp]
    if peak_rows.empty:
        raise ValueError("Confirmed history does not include the active peak date.")

    peak_date = active_cycle.peak_date
    peak_price = float(peak_rows.iloc[-1]["close"])
    saw_below = False
    changed = False
    for row in frame.loc[frame["date"] > peak_timestamp].itertuples(index=False):
        current_date = row.date.date()
        current_price = float(row.close)
        equal_peak = math.isclose(
            current_price,
            peak_price,
            rel_tol=_PRICE_RELATIVE_TOLERANCE,
            abs_tol=_PRICE_ABSOLUTE_TOLERANCE,
        )
        if current_price > peak_price and not equal_peak:
            peak_date = current_date
            peak_price = current_price
            saw_below = False
            changed = True
        elif equal_peak and saw_below:
            peak_date = current_date
            peak_price = current_price
            saw_below = False
            changed = True
        elif current_price < peak_price and not equal_peak:
            saw_below = True
    return peak_date, peak_price, changed


def _require_positive_finite(value: object, name: str) -> None:
    number = _read_finite_number(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive.")


def _has_positive_activity(volume: object, amount: object) -> bool:
    for value in (volume, amount):
        try:
            if value is not None and math.isfinite(float(value)) and float(value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _format_amount(amount: int | float) -> str:
    if float(amount).is_integer():
        return f"¥{amount:,.0f}"
    return f"¥{amount:,.2f}"


def _format_percent(value: float) -> str:
    return f"{value * 100:g}%"


def _tier_command_text(tiers: Sequence[DrawdownTier]) -> str:
    return ",".join(f"{tier.drawdown * 100:g}" for tier in tiers)


def _format_trend(
    config: DrawdownPlanConfig,
    evaluation: DrawdownPlanEvaluation,
) -> tuple[str, ...]:
    label = f"MA{config.sma_window}"
    if evaluation.sma is None:
        return (f"{label}: unavailable (insufficient history)",)

    lines = (
        f"{label}: {evaluation.sma:.6g}",
        f"Price vs {label}: {evaluation.distance_to_sma:+.1%}",
    )
    if evaluation.sma_slope is None:
        return (*lines, f"{label} trend: unavailable (insufficient history)")
    direction = "rising" if evaluation.sma_slope > 0 else "falling"
    if math.isclose(evaluation.sma_slope, 0, abs_tol=_THRESHOLD_TOLERANCE):
        direction = "flat"
    return (
        *lines,
        f"{label} {config.sma_slope_window}-session slope: "
        f"{direction} ({evaluation.sma_slope:+.1%})",
    )
