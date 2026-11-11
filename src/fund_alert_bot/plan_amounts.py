"""State-aware amount edits; market facts and purchase estimates stay intact."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fund_alert_bot.db import (
    get_active_drawdown_cycle,
    get_drawdown_tier_reminder_states,
    get_fund_settings,
    list_drawdown_tier_records,
    list_manual_add_actions,
    write_transaction,
)
from fund_alert_bot.rules.drawdown_plan import (
    TIER_STATE_ADDED,
    TIER_STATE_PENDING,
    TIER_STATE_SKIPPED,
    TIER_STATE_UNTRIGGERED,
    DrawdownPlanConfig,
    DrawdownTier,
    derive_tier_action_state,
    format_plan_amount,
    format_plan_percent,
    parse_drawdown_plan_config,
    validate_drawdown_plan_notification_size,
)


def amount_revision(connection: sqlite3.Connection, rule_id: int) -> int:
    """Return an edit token, including edits that restore an earlier amount."""

    return int(
        connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM drawdown_plan_amount_changes "
            "WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()[0]
    )


def cycle_amount_config(
    connection: sqlite3.Connection,
    config: DrawdownPlanConfig,
    cycle_id: int,
    *,
    on_date: date | None = None,
) -> DrawdownPlanConfig:
    """Resolve cycle amounts from original facts and dated explicit edits."""

    amounts = {
        str(row["tier_key"]): float(row["amount"])
        for row in list_drawdown_tier_records(connection, cycle_id)
    }
    rows = connection.execute(
        """
        SELECT tier_key, cycle_amount FROM drawdown_plan_amount_changes
        WHERE cycle_id = ? AND (? IS NULL OR effective_date <= ?)
        ORDER BY id
        """,
        (
            cycle_id,
            None if on_date is None else on_date.isoformat(),
            None if on_date is None else on_date.isoformat(),
        ),
    ).fetchall()
    for row in rows:
        amounts[str(row["tier_key"])] = float(row["cycle_amount"])
    return replace(
        config,
        tiers=tuple(
            replace(tier, amount=amounts.get(tier.key, tier.amount))
            for tier in config.tiers
        ),
    )


@dataclass(frozen=True, slots=True)
class AmountChange:
    tier: DrawdownTier
    plan_amount: int | float
    cycle_amount: int | float
    state: str


@dataclass(frozen=True, slots=True)
class PlanAmountPreview:
    rule_id: int
    name: str
    cycle_id: int | None
    requested: tuple[DrawdownTier, ...]
    changes: tuple[AmountChange, ...]
    fingerprint: str
    future_total: int | float
    pending_total: int | float
    revised_pending_total: int | float

    @property
    def changes_pending(self) -> bool:
        return any(
            change.state == TIER_STATE_PENDING
            and change.cycle_amount != change.tier.amount
            for change in self.changes
        )


def preview_plan_amounts(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    requested: tuple[DrawdownTier, ...],
) -> PlanAmountPreview:
    """Load a local-only preview; the fingerprint binds confirmation to state."""

    rule = connection.execute(
        "SELECT * FROM rules WHERE id = ? AND type = 'drawdown_plan' AND enabled = 1",
        (rule_id,),
    ).fetchone()
    if rule is None:
        raise ValueError("Enabled drawdown plan was not found.")
    params = json.loads(str(rule["params_json"]))
    config = parse_drawdown_plan_config(
        reference_symbol=str(rule["symbol"]),
        asset_type=str(rule["asset_type"]),
        params=params,
    )
    requested_by_key = {tier.key: tier for tier in requested}
    if not requested or len(requested_by_key) != len(requested):
        raise ValueError("Select unique existing tiers with positive amounts.")
    if not set(requested_by_key).issubset(tier.key for tier in config.tiers):
        raise ValueError(
            "Only amounts of existing tiers can change; keep drawdown percentages."
        )
    new_params = dict(params)
    new_params["tiers"] = [
        {
            "drawdown": tier.drawdown,
            "amount": requested_by_key.get(tier.key, tier).amount,
        }
        for tier in config.tiers
    ]
    revised = parse_drawdown_plan_config(
        reference_symbol=str(rule["symbol"]),
        asset_type=str(rule["asset_type"]),
        params=new_params,
    )
    validate_drawdown_plan_notification_size(
        name=str(rule["name"]),
        reference_symbol=str(rule["symbol"]),
        config=revised,
    )
    settings = get_fund_settings(connection, config.investment_fund_symbol)
    if (
        settings is not None
        and settings["fee_mode"] == "fixed"
        and any(
            float(tier.amount) <= float(settings["fee_value"]) for tier in revised.tiers
        )
    ):
        raise ValueError("Fixed fee must be lower than each tier amount.")
    active = get_active_drawdown_cycle(connection, rule_id)
    cycle_id = None if active is None else int(active["id"])
    records = (
        [] if cycle_id is None else list_drawdown_tier_records(connection, cycle_id)
    )
    actions = [] if cycle_id is None else list_manual_add_actions(connection, cycle_id)
    reminders = (
        {}
        if cycle_id is None
        else get_drawdown_tier_reminder_states(connection, cycle_id)
    )
    effective = (
        config
        if cycle_id is None
        else cycle_amount_config(connection, config, cycle_id)
    )
    states = {
        tier.key: derive_tier_action_state(
            tier_key=tier.key,
            triggered_tier_keys={str(row["tier_key"]) for row in records},
            added_tier_keys={str(row["tier_key"]) for row in actions},
            skipped_tier_keys={
                key for key, row in reminders.items() if row["skipped_for_cycle"]
            },
        )
        for tier in config.tiers
    }
    old_plan = {tier.key: tier for tier in config.tiers}
    old_cycle = {tier.key: tier for tier in effective.tiers}
    if any(tier.drawdown != old_plan[tier.key].drawdown for tier in requested):
        raise ValueError(
            "Only amounts of existing tiers can change; keep drawdown percentages."
        )
    changes = tuple(
        AmountChange(
            tier,
            old_plan[tier.key].amount,
            old_cycle[tier.key].amount,
            states[tier.key],
        )
        for tier in requested
        if tier.amount != old_plan[tier.key].amount
        or (
            states[tier.key] == TIER_STATE_PENDING
            and tier.amount != old_cycle[tier.key].amount
        )
    )
    if not changes:
        raise ValueError("The selected amounts are already configured.")
    # Mixed current/future amounts must also fit notification and numeric limits.
    mixed_params = dict(new_params)
    for update_pending in (False, True):
        mixed_params["tiers"] = [
            {
                "drawdown": tier.drawdown,
                "amount": (
                    requested_by_key[tier.key].amount
                    if tier.key in requested_by_key
                    and (
                        states[tier.key] == TIER_STATE_UNTRIGGERED
                        or (update_pending and states[tier.key] == TIER_STATE_PENDING)
                    )
                    else tier.amount
                ),
            }
            for tier in effective.tiers
        ]
        mixed = parse_drawdown_plan_config(
            reference_symbol=str(rule["symbol"]),
            asset_type=str(rule["asset_type"]),
            params=mixed_params,
        )
        validate_drawdown_plan_notification_size(
            name=str(rule["name"]),
            reference_symbol=str(rule["symbol"]),
            config=mixed,
        )
    snapshot = {
        "rule": dict(rule),
        "cycle": None if active is None else dict(active),
        "records": [dict(row) for row in records],
        "actions": [dict(row) for row in actions],
        "reminders": [dict(row) for row in reminders.values()],
        "settings": None if settings is None else dict(settings),
        "revision": amount_revision(connection, rule_id),
    }
    return PlanAmountPreview(
        rule_id,
        str(rule["name"]),
        cycle_id,
        requested,
        changes,
        hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        sum(tier.amount for tier in revised.tiers),
        sum(
            tier.amount
            for tier in effective.tiers
            if states[tier.key] == TIER_STATE_PENDING
        ),
        sum(
            requested_by_key.get(tier.key, tier).amount
            for tier in effective.tiers
            if states[tier.key] == TIER_STATE_PENDING
        ),
    )


def apply_plan_amounts(
    connection: sqlite3.Connection,
    preview: PlanAmountPreview,
    *,
    update_pending: bool,
    confirmed_unpurchased: bool,
    now: datetime,
) -> None:
    """Revalidate and commit the configuration and dated cycle amounts together."""

    if preview.changes_pending and update_pending and not confirmed_unpurchased:
        raise ValueError(
            "Confirm that the selected pending tiers have not been purchased."
        )
    with write_transaction(connection):
        current = preview_plan_amounts(
            connection, rule_id=preview.rule_id, requested=preview.requested
        )
        if current.fingerprint != preview.fingerprint:
            raise ValueError(
                "Plan state changed. Rerun /set_plan_amounts for a fresh preview."
            )
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (preview.rule_id,)
        ).fetchone()
        params = json.loads(str(rule["params_json"]))
        updates = {tier.key: tier.amount for tier in preview.requested}
        config = parse_drawdown_plan_config(
            reference_symbol=str(rule["symbol"]),
            asset_type=str(rule["asset_type"]),
            params=params,
        )
        params["tiers"] = [
            {"drawdown": tier.drawdown, "amount": updates.get(tier.key, tier.amount)}
            for tier in config.tiers
        ]
        timestamp = now.isoformat()
        effective_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        connection.execute(
            "UPDATE rules SET params_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(params, ensure_ascii=False), timestamp, preview.rule_id),
        )
        for change in preview.changes:
            cycle_amount = change.cycle_amount
            if change.state == TIER_STATE_UNTRIGGERED or (
                update_pending and change.state == TIER_STATE_PENDING
            ):
                cycle_amount = change.tier.amount
            connection.execute(
                """
                INSERT INTO drawdown_plan_amount_changes
                    (rule_id, cycle_id, tier_key, plan_amount, cycle_amount,
                     effective_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preview.rule_id,
                    preview.cycle_id,
                    change.tier.key,
                    change.tier.amount,
                    cycle_amount,
                    effective_date,
                    timestamp,
                ),
            )
        # Old market snapshots remain readable, but their delivery/actions expire.
        # Revoke claims as well: a late sender must not re-enable an old retry.
        event_filter = """
            SELECT id FROM alert_events WHERE rule_id = ? AND (
                json_type(payload_json, '$.actionable_tiers') = 'array'
                OR json_type(payload_json, '$.crossed_tiers') = 'array'
            )
        """
        connection.execute(
            f"""UPDATE notification_deliveries SET status = 'cancelled',
                claim_token = NULL, claim_until = NULL, updated_at = ?
                WHERE event_id IN ({event_filter})
                AND status IN ('pending', 'failed', 'sending')""",
            (timestamp, preview.rule_id),
        )
        connection.execute(
            f"""UPDATE alert_events SET notification_status = 'cancelled'
                WHERE id IN ({event_filter}) AND notification_status != 'sent'""",
            (preview.rule_id,),
        )


def format_amount_preview(preview: PlanAmountPreview) -> str:
    lines = [
        "Preview drawdown tier amounts",
        f"Plan: {preview.name} ({preview.rule_id})",
    ]
    for change in preview.changes:
        timing = {
            TIER_STATE_UNTRIGGERED: "Current cycle and future cycles",
            TIER_STATE_PENDING: "Choose whether to update this pending amount",
            TIER_STATE_ADDED: "Purchase unchanged; next cycle only",
            TIER_STATE_SKIPPED: "Still skipped; next cycle only",
        }[change.state]
        lines.extend(
            (
                f"• -{format_plan_percent(change.tier.drawdown)} [{change.state}]",
                f"Plan amount: {format_plan_amount(change.plan_amount)} → "
                f"{format_plan_amount(change.tier.amount)}",
                f"Current-cycle amount: {format_plan_amount(change.cycle_amount)}",
                timing,
            )
        )
    lines.extend(
        (
            f"Future one-cycle total: {format_plan_amount(preview.future_total)}",
            "Pending total (keep current amounts): "
            f"{format_plan_amount(preview.pending_total)}",
            "Pending total (update selected pending tiers): "
            f"{format_plan_amount(preview.revised_pending_total)}",
            "Already submitted a subscription? "
            "Record it with /mark_added before editing.",
            "Trigger facts, purchases, skips and position estimates are preserved.",
            "Old reminder buttons expire after saving; "
            "use /mark_added to record a purchase.",
        )
    )
    return "\n".join(lines)
