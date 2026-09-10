"""Render supplied investment-plan snapshots without fetching or changing state."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fund_alert_bot.i18n import get_language
from fund_alert_bot.rules.drawdown_plan import (
    TIER_STATE_ADDED,
    TIER_STATE_PENDING,
    TIER_STATE_SKIPPED,
    derive_tier_action_state,
    format_plan_amount,
    format_plan_percent,
)
from fund_alert_bot.rules.profit import format_profit_threshold_key

if TYPE_CHECKING:
    from fund_alert_bot.checks import DrawdownPlanStatus, DrawdownPlanStatusResult
    from fund_alert_bot.market_data.models import FundNav

# Preserve the logger category used by existing command logging configuration.
LOGGER = logging.getLogger("fund_alert_bot.commands")


def format_plan_overview(
    result: DrawdownPlanStatusResult,
    unmatched_positions: Sequence[tuple[Any, FundNav | None, str, str | None]] = (),
    dca_statuses: Sequence[Any] = (),
    profit_statuses: Sequence[Any] = (),
    profit_setup_funds: Sequence[tuple[str, str]] = (),
) -> str:
    """Format concise `/plans` output."""

    if (
        not result.statuses
        and not unmatched_positions
        and not dca_statuses
        and not profit_statuses
        and not profit_setup_funds
        and not result.no_data_skips
        and not result.errors
    ):
        return "No investment plans or positions configured."
    lines = ["📊 Investment Plans"]
    for row in profit_statuses:
        try:
            params = _load_params(str(row["params_json"]))
            thresholds = [float(value) for value in params["thresholds"]]
            threshold_keys = [
                format_profit_threshold_key(value) for value in thresholds
            ]
            if (
                not thresholds
                or any(
                    not math.isfinite(value) or value <= 0 or value >= 1
                    for value in thresholds
                )
                or thresholds != sorted(thresholds)
                or len(threshold_keys) != len(set(threshold_keys))
            ):
                raise ValueError("invalid thresholds")
        except (KeyError, TypeError, ValueError):
            LOGGER.warning(
                "Skipping malformed Price-Gain status rule_id=%s", row["rule_id"]
            )
            continue
        lines.extend(
            (
                "",
                f"{row['name']} (Price-Gain {row['rule_id']})",
                f"Fund {row['fund_symbol']} / auto position cost",
                "Thresholds: "
                + ", ".join(format_plan_percent(value) for value in thresholds),
            )
        )
        if (
            row["snapshot_sync_required_since"] is not None
            or row["settings_sync_required_since"] is not None
        ):
            lines.append(
                "Position Sync required — reminders paused; run /sync_position"
            )
        elif row["units"] is None:
            lines.append("Position: unavailable — remember /sync_position")
        elif float(row["units"]) == 0:
            lines.append("Position: closed (exact zero units)")
        elif row["position_cycle_id"] is None:
            lines.append("Position cycle unavailable — rerun /sync_position")
        else:
            accuracy = "estimated" if row["is_estimated"] else "exact"
            lines.append(
                f"Position: {accuracy}; average cost "
                f"{float(row['average_unit_cost']):.6f}; "
                f"reached {row['reached_thresholds']}/{len(thresholds)}"
            )
    for row in dca_statuses:
        params = _load_params(str(row["params_json"]))
        lines.extend(
            (
                "",
                f"{row['name']} (fixed DCA {row['rule_id']})",
                f"Fund {row['fund_symbol']} / every {params['weekday']} / "
                f"{format_plan_amount(float(params['amount']))}",
                f"Holiday policy: {params['holiday_policy']}",
            )
        )
        if row["due_date"] is None:
            lines.append("Latest occurrence: none yet")
        else:
            effective = (
                "unresolved"
                if row["effective_date"] is None
                else str(row["effective_date"])
            )
            lines.append(
                f"Latest occurrence: {row['due_date']} / {row['status']} / "
                f"NAV date {effective}"
            )
            if row["added_units"] is not None:
                lines.append(f"Estimated units added: {float(row['added_units']):.6f}")
            if row["status"] == "pending":
                if row["effective_date"] is None:
                    lines.append(
                        "If executed, wait for a confirmed subscription NAV date."
                    )
                lines.extend(
                    (
                        "If the deduction failed, use:",
                        f"/dca_skip {row['rule_id']} {row['due_date']}",
                    )
                )
            elif row["status"] in {"applied", "skipped", "reconciled_by_sync"}:
                lines.append(
                    "Next action: none; use /sync_position only if the "
                    "platform differs."
                )
        if row["last_synced_at"] is None:
            lines.append("Position: not synced — remember /sync_position")
        else:
            accuracy = "estimated" if row["is_estimated"] else "exact"
            lines.append(
                f"Position: {accuracy}; last sync {row['last_synced_at']}; "
                f"later estimates {row['estimates_since_sync']}"
            )
    for status in result.statuses:
        next_tier = _next_open_tier(status)
        pending_tiers = _pending_drawdown_tiers(status)
        newly_reached_tiers = _currently_reached_unrecorded_tiers(status)
        plan_lines = [
            "",
            f"{status.name} (plan {status.rule_id}) — {status.readiness}",
            f"ETF {status.reference_symbol} → fund "
            f"{status.config.investment_fund_symbol}",
            "Rearm: +"
            f"{format_plan_percent(status.config.rearm_margin)} from cycle anchor",
            f"Drawdown: {_format_plan_drawdown(status.evaluation.drawdown)} "
            f"({status.evaluation.latest_date}, {status.evaluation.source})",
        ]
        if pending_tiers:
            plan_lines.append(
                "⏳ Triggered, still pending: "
                + ", ".join(
                    f"-{format_plan_percent(tier.drawdown)} / "
                    f"{format_plan_amount(tier.amount)}"
                    for tier in pending_tiers
                )
            )
            tier_text = ",".join(
                format_plan_percent(tier.key).removesuffix("%")
                for tier in pending_tiers
            )
            plan_lines.append(
                "If you actually subscribed, record it using the actual date: "
                f"/mark_added {status.rule_id} {tier_text} <YYYY-MM-DD>"
            )
        if newly_reached_tiers:
            plan_lines.append(
                "⚠️ Reached, awaiting official close confirmation: "
                + ", ".join(
                    f"-{format_plan_percent(tier.drawdown)} / "
                    f"{format_plan_amount(tier.amount)}"
                    for tier in newly_reached_tiers
                )
            )
        skipped_tiers = tuple(
            tier for tier in status.config.tiers if tier.key in status.skipped_tier_keys
        )
        if skipped_tiers:
            plan_lines.append(
                "⏭ Skipped for this cycle: "
                + ", ".join(
                    f"-{format_plan_percent(tier.drawdown)} / "
                    f"{format_plan_amount(tier.amount)}"
                    for tier in skipped_tiers
                )
            )
        plan_lines.append(
            "Next open tier: all tiers already reminded"
            if next_tier is None
            else "Next open tier: "
            f"-{format_plan_percent(next_tier.drawdown)} / "
            f"{format_plan_amount(next_tier.amount)}"
        )
        plan_lines.extend(_format_position_lines(status))
        lines.extend(plan_lines)
    for position, nav, ownership, nav_unavailable_reason in unmatched_positions:
        units = float(position["units"])
        accuracy = "estimated" if position["is_estimated"] else "exact"
        lines.extend(
            (
                "",
                f"Fund {position['fund_symbol']} — {ownership}",
                f"Position: {accuracy}; last sync {position['last_synced_at']}; "
                f"later estimates {position['estimates_since_sync']}",
            )
        )
        if (
            position["position_sync_required_since"] is not None
            or position["settings_sync_required_since"] is not None
        ):
            lines.append(
                "Position Sync required — reminders paused; run /sync_position"
            )
        if units == 0:
            lines.append("Position value: ¥0.00 (closed)")
        elif nav is None:
            reason = (
                f": {nav_unavailable_reason}"
                if nav_unavailable_reason is not None
                else " (dated fund NAV missing)"
            )
            lines.append(f"Position value: unavailable{reason}")
        else:
            lines.append(
                f"Position value: ¥{units * nav.value:,.2f} using NAV "
                f"{nav.value:.12g} on {nav.date}"
            )
    for fund_symbol, name in profit_setup_funds:
        safe_name = re.sub(r"\s+", "_", name.strip()) or fund_symbol
        lines.extend(
            (
                "",
                f"Price-Gain setup available for {fund_symbol} / {name}",
                "Template: /add_profit cn_open_fund "
                f"{fund_symbol} {safe_name} auto <thresholds，例如20,30>",
            )
        )
    _append_plan_failures(lines, result)
    return "\n".join(lines)


def format_plan_details(result: DrawdownPlanStatusResult) -> str:
    """Format detailed read-only plan state for `/check`."""

    if not result.statuses and not result.no_data_skips and not result.errors:
        return ""
    lines = ["", "📉 Drawdown Add Plan status (read-only)"]
    for status in result.statuses:
        evaluation = status.evaluation
        rearm_threshold = evaluation.initial_peak_price * (
            1 + status.config.rearm_margin
        )
        lines.extend(
            (
                "",
                f"{status.name} (plan {status.rule_id})",
                f"Reference ETF: {status.reference_symbol}",
                f"Investment fund: {status.config.investment_fund_symbol}",
                f"Data: {evaluation.latest_date} / {evaluation.source} qfq close",
                f"Current: {evaluation.latest_price:.6g}",
                f"Cycle anchor: {evaluation.initial_peak_price:.6g} on "
                f"{evaluation.initial_peak_date}",
                f"Current peak: {evaluation.peak_price:.6g} on {evaluation.peak_date}",
                f"Rearm margin: {format_plan_percent(status.config.rearm_margin)}",
                f"Rearm threshold: {rearm_threshold:.6g}",
                "Rearm occurs only on a future confirmed new peak.",
                f"Drawdown: {_format_plan_drawdown(evaluation.drawdown)}",
                *_format_plan_trend(status),
                f"Readiness: {status.readiness}",
            )
        )
        if status.missing_setup:
            lines.append(f"Missing setup: {', '.join(status.missing_setup)}")
        lines.append("Tiers:")
        language = get_language()
        for tier in status.config.tiers:
            state_key = derive_tier_action_state(
                tier_key=tier.key,
                triggered_tier_keys=status.recorded_tier_keys,
                added_tier_keys=status.added_tier_keys,
                skipped_tier_keys=status.skipped_tier_keys,
            )
            if state_key == TIER_STATE_ADDED:
                state = (
                    "add recorded (user-confirmed) [ADDED]"
                    if language == "en"
                    else "已加仓（ADDED）"
                )
            elif state_key == TIER_STATE_SKIPPED:
                state = (
                    "skipped (this cycle) [SKIPPED]"
                    if language == "en"
                    else "已跳过（SKIPPED）"
                )
            elif state_key == TIER_STATE_PENDING:
                state = (
                    "reminded; no add recorded [TRIGGERED_PENDING]"
                    if language == "en"
                    else "已触发待处理（TRIGGERED_PENDING）"
                )
                if tier.key in status.snoozed_tier_keys:
                    if language == "en":
                        deferred = "; deferred today"
                    else:
                        deferred = "；今天已顺延提醒"
                    state += deferred
            else:
                state = (
                    "open [UNTRIGGERED]"
                    if language == "en"
                    else "未触发（UNTRIGGERED）"
                )
            lines.append(
                f"• -{format_plan_percent(tier.drawdown)} / "
                f"{format_plan_amount(tier.amount)}: {state}"
            )
        next_tier = _next_open_tier(status)
        if next_tier is None:
            lines.append("Next level: all tiers already reminded")
        else:
            distance = max(0.0, next_tier.drawdown - evaluation.drawdown)
            lines.extend(
                (
                    f"Next level: -{format_plan_percent(next_tier.drawdown)}",
                    f"Distance to next level: {distance * 100:.1f} percentage points",
                )
            )
        lines.extend(_format_position_lines(status))
    _append_plan_failures(lines, result)
    return "\n".join(lines)


def _format_plan_drawdown(drawdown: float) -> str:
    """Format a non-negative drawdown without displaying negative zero."""

    return f"{-drawdown if drawdown > 0 else 0.0:.1%}"


def _next_open_tier(status: DrawdownPlanStatus) -> Any | None:
    completed = status.recorded_tier_keys | (
        status.added_tier_keys | status.skipped_tier_keys
    )
    return next(
        (tier for tier in status.config.tiers if tier.key not in completed),
        None,
    )


def _currently_reached_unrecorded_tiers(
    status: DrawdownPlanStatus,
) -> tuple[Any, ...]:
    """Return newly reached tiers that still await close confirmation."""

    completed = status.recorded_tier_keys | (
        status.added_tier_keys | status.skipped_tier_keys
    )
    return tuple(
        tier
        for tier in status.config.tiers
        if tier.key not in completed
        and status.evaluation.drawdown + 1e-12 >= tier.drawdown
    )


def _pending_drawdown_tiers(status: DrawdownPlanStatus) -> tuple[Any, ...]:
    """Return close-confirmed tiers that have no user action yet."""

    return tuple(
        tier
        for tier in status.config.tiers
        if tier.key in status.recorded_tier_keys
        and tier.key not in status.added_tier_keys
        and tier.key not in status.skipped_tier_keys
    )


def _format_plan_trend(status: DrawdownPlanStatus) -> tuple[str, ...]:
    evaluation = status.evaluation
    label = f"MA{status.config.sma_window}"
    if evaluation.sma is None:
        return (f"{label}: unavailable (insufficient history)",)
    lines = [
        f"{label}: {evaluation.sma:.6g}",
        f"Price vs {label}: {evaluation.distance_to_sma:+.1%}",
    ]
    if evaluation.sma_slope is None:
        lines.append(f"{label} slope: unavailable (insufficient history)")
    else:
        direction = "rising" if evaluation.sma_slope > 0 else "falling"
        if math.isclose(evaluation.sma_slope, 0, abs_tol=1e-12):
            direction = "flat"
        lines.append(
            f"{label} {status.config.sma_slope_window}-session slope: "
            f"{direction} ({evaluation.sma_slope:+.1%})"
        )
    return tuple(lines)


def _format_position_lines(status: DrawdownPlanStatus) -> tuple[str, ...]:
    if status.position is None:
        return (
            "Position: not synced",
            *(
                (f"Position sync required since {status.position_sync_required_since}",)
                if status.position_sync_required_since is not None
                else ()
            ),
        )
    accuracy = "estimated" if status.position["is_estimated"] else "exact"
    units = float(status.position["units"])
    lines = [
        f"Position: {accuracy}; last sync {status.position['last_synced_at']}; "
        f"later estimates {status.position['estimates_since_sync']}"
    ]
    if status.position_sync_required_since is not None:
        lines.append(
            f"Position sync required since {status.position_sync_required_since}"
        )
    if units == 0:
        lines.append("Position value: ¥0.00 (closed)")
    elif status.fund_nav is None:
        if status.fund_nav_unavailable_reason is None:
            lines.append("Position value: unavailable (dated fund NAV missing)")
        else:
            reason = status.fund_nav_unavailable_reason
            lines.append(f"Position value: unavailable: {reason}")
    else:
        lines.append(
            f"Position value: ¥{units * status.fund_nav.value:,.2f} using NAV "
            f"{status.fund_nav.value:.12g} on {status.fund_nav.date}"
        )
    return tuple(lines)


def _append_plan_failures(
    lines: list[str],
    result: DrawdownPlanStatusResult,
) -> None:
    for skip in result.no_data_skips:
        lines.append(f"⚠️ {skip.symbol}: data unavailable — {skip.message}")
    for error in result.errors:
        lines.append(f"❌ Plan {error.rule_id} {error.symbol}: {error.message}")


def _load_params(params_json: str) -> dict[str, Any]:
    params = json.loads(params_json)
    if not isinstance(params, dict):
        raise ValueError("params_json must contain a JSON object")
    return params
