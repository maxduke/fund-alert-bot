"""Conservative SQLite retention and pruning helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from fund_alert_bot.rules.drawdown import MAX_WINDOW_DAYS

_PRUNE_TABLES = (
    "market_daily_history",
    "fund_nav_history",
    "scheduled_dca_occurrences",
    "manual_add_estimates",
    "manual_add_actions",
    "position_profit_evaluations",
    "position_profit_thresholds",
    "position_cycles",
    "drawdown_tier_records",
    "drawdown_tier_reminder_states",
    "drawdown_cycles",
    "alert_events",
)


def prune_database(
    connection: sqlite3.Connection,
    *,
    today: date,
    retention_days: int | None = None,
) -> dict[str, int]:
    """Prune bounded historical state without touching active work.

    The operation is one SQLite transaction.  A savepoint also makes the
    helper safe to call from a caller-owned transaction without committing
    unrelated work.
    """
    from fund_alert_bot.db import DEFAULT_RETENTION_DAYS, write_transaction

    days = DEFAULT_RETENTION_DAYS if retention_days is None else retention_days
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError("retention_days must be a positive integer.")
    if days <= 0:
        raise ValueError("retention_days must be a positive integer.")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date.")

    cutoff = today - timedelta(days=days)
    counts = {table: 0 for table in _PRUNE_TABLES}

    def run_prune() -> None:
        _cancel_expired_before_close_events(connection, today=today)
        counts["market_daily_history"] = _prune_market_daily_history(
            connection,
            today=today,
        )
        counts["fund_nav_history"] = _prune_fund_nav_history(
            connection,
            cutoff=cutoff,
        )
        counts["scheduled_dca_occurrences"] = _prune_scheduled_dca_occurrences(
            connection,
            cutoff=cutoff,
        )
        counts["position_profit_evaluations"] += _prune_active_position_evaluations(
            connection,
        )
        (
            thresholds_deleted,
            evaluations_deleted,
            cycles_deleted,
        ) = _prune_ended_position_cycles(connection, cutoff=cutoff)
        counts["position_profit_thresholds"] += thresholds_deleted
        counts["position_profit_evaluations"] += evaluations_deleted
        counts["position_cycles"] += cycles_deleted
        (
            counts["manual_add_actions"],
            counts["manual_add_estimates"],
            counts["drawdown_tier_reminder_states"],
            counts["drawdown_tier_records"],
            counts["drawdown_cycles"],
        ) = _prune_ended_drawdown_cycles(connection, cutoff=cutoff)
        counts["alert_events"] = _prune_alert_events(
            connection,
            today=today,
            cutoff=cutoff,
        )

    if connection.in_transaction:
        connection.execute("SAVEPOINT prune_database")
        try:
            run_prune()
            connection.execute("RELEASE SAVEPOINT prune_database")
        except BaseException:
            connection.execute("ROLLBACK TO SAVEPOINT prune_database")
            connection.execute("RELEASE SAVEPOINT prune_database")
            raise
    else:
        with write_transaction(connection):
            run_prune()
    return counts


def _cancel_expired_before_close_events(
    connection: sqlite3.Connection,
    *,
    today: date,
) -> int:
    """Cancel unfinished before-close pre-alerts from earlier dates."""
    from fund_alert_bot.db import (
        ALERT_NOTIFICATION_CANCELLED,
        ALERT_NOTIFICATION_FAILED,
        ALERT_NOTIFICATION_PENDING,
        NOTIFICATION_DELIVERY_CANCELLED,
        NOTIFICATION_DELIVERY_FAILED,
        NOTIFICATION_DELIVERY_PENDING,
        NOTIFICATION_DELIVERY_SENDING,
        _utc_now_text,
    )

    now = _utc_now_text()
    today_text = today.isoformat()
    rows = connection.execute(
        """
        SELECT id
        FROM alert_events
        WHERE notification_status IN (?, ?)
            AND json_valid(payload_json)
            AND json_extract(payload_json, '$.phase') = 'before_close'
            AND COALESCE(
                json_extract(payload_json, '$.data_date'),
                substr(triggered_at, 1, 10)
            ) < ?
        """,
        (ALERT_NOTIFICATION_PENDING, ALERT_NOTIFICATION_FAILED, today_text),
    ).fetchall()
    event_ids = tuple(int(row["id"]) for row in rows)
    if not event_ids:
        return 0
    placeholders = ", ".join("?" for _ in event_ids)
    connection.execute(
        f"""
        UPDATE notification_deliveries
        SET status = ?,
            claim_token = NULL,
            claim_until = NULL,
            updated_at = ?
        WHERE event_id IN ({placeholders})
            AND status IN (?, ?, ?)
        """,
        (
            NOTIFICATION_DELIVERY_CANCELLED,
            now,
            *event_ids,
            NOTIFICATION_DELIVERY_PENDING,
            NOTIFICATION_DELIVERY_FAILED,
            NOTIFICATION_DELIVERY_SENDING,
        ),
    )
    connection.execute(
        f"""
        UPDATE alert_events
        SET notification_status = ?
        WHERE id IN ({placeholders})
            AND notification_status IN (?, ?)
        """,
        (
            ALERT_NOTIFICATION_CANCELLED,
            *event_ids,
            ALERT_NOTIFICATION_PENDING,
            ALERT_NOTIFICATION_FAILED,
        ),
    )
    return len(event_ids)


def _prune_market_daily_history(
    connection: sqlite3.Connection,
    *,
    today: date,
) -> int:
    """Keep only the ranges required by currently enabled history rules."""

    connection.execute(
        """
        UPDATE drawdown_cycles
        SET saw_below_peak = 1
        WHERE end_date IS NULL
            AND saw_below_peak = 0
            AND EXISTS (
                SELECT 1
                FROM rules AS r
                JOIN market_daily_history AS h
                    ON h.symbol = r.symbol
                    AND h.asset_type = r.asset_type
                    AND h.price_basis = 'qfq'
                WHERE r.id = drawdown_cycles.rule_id
                    AND h.date > drawdown_cycles.peak_date
                    AND h.date <= drawdown_cycles.last_evaluated_date
                    AND h.close < drawdown_cycles.peak_price
            )
        """
    )

    windows: dict[tuple[str, str, str], tuple[date, set[date]]] = {}
    rules = connection.execute(
        """
        SELECT id, type, symbol, asset_type, params_json
        FROM rules
        WHERE enabled = 1
        """
    ).fetchall()
    for rule in rules:
        try:
            params = json.loads(str(rule["params_json"]))
            if not isinstance(params, dict):
                continue
            rule_type = str(rule["type"])
            if rule_type in {"drawdown_from_high", "drawdown"}:
                lookback = _positive_rule_int(params, "lookback_days")
                lower = today - timedelta(days=lookback + 14)
                key = (str(rule["symbol"]), str(rule["asset_type"]), "unadjusted")
            elif rule_type == "drawdown_plan":
                lookback = _positive_rule_int(params, "lookback_days", default=365)
                sma_window = _positive_rule_int(params, "sma_window", default=250)
                slope_window = _positive_rule_int(
                    params,
                    "sma_slope_window",
                    default=20,
                )
                calendar_days = max(lookback, 2 * (sma_window + slope_window))
                lower = today - timedelta(days=calendar_days + 14)
                key = (str(rule["symbol"]), str(rule["asset_type"]), "qfq")
                active_cycles = connection.execute(
                    """
                    SELECT initial_peak_date, peak_date
                    FROM drawdown_cycles
                    WHERE rule_id = ? AND end_date IS NULL
                    """,
                    (int(rule["id"]),),
                ).fetchall()
            else:
                continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        previous = windows.get(key)
        if previous is None:
            windows[key] = (lower, set())
        elif lower < previous[0]:
            windows[key] = (lower, previous[1])

        if rule_type == "drawdown_plan":
            extra_dates = windows[key][1]
            for cycle in active_cycles:
                for cycle_date in (
                    _parse_storage_date(cycle["initial_peak_date"]),
                    _parse_storage_date(cycle["peak_date"]),
                ):
                    if cycle_date is not None:
                        extra_dates.add(cycle_date)

    if not windows:
        cursor = connection.execute("DELETE FROM market_daily_history")
        return cursor.rowcount

    predicates: list[str] = []
    values: list[object] = []
    for (symbol, asset_type, price_basis), (lower, extra_dates) in windows.items():
        date_predicate = "date >= ?"
        date_values: list[object] = [lower.isoformat()]
        if extra_dates:
            placeholders = ", ".join("?" for _ in extra_dates)
            date_predicate += f" OR date IN ({placeholders})"
            date_values.extend(sorted(value.isoformat() for value in extra_dates))
        predicates.append(
            "(symbol = ? AND asset_type = ? AND price_basis = ? AND ("
            + date_predicate
            + "))"
        )
        values.extend((symbol, asset_type, price_basis, *date_values))
    cursor = connection.execute(
        "DELETE FROM market_daily_history WHERE NOT (" + " OR ".join(predicates) + ")",
        values,
    )
    return cursor.rowcount


def _prune_fund_nav_history(
    connection: sqlite3.Connection,
    *,
    cutoff: date,
) -> int:
    """Keep recent NAVs, one latest row per fund, and pending exact dates."""

    cursor = connection.execute(
        """
        DELETE FROM fund_nav_history
        WHERE nav_date < ?
            AND nav_date != (
                SELECT MAX(latest.nav_date)
                FROM fund_nav_history AS latest
                WHERE latest.fund_symbol = fund_nav_history.fund_symbol
            )
            AND NOT EXISTS (
                SELECT 1
                FROM manual_add_estimates AS m
                WHERE m.status = 'pending'
                    AND m.fund_symbol = fund_nav_history.fund_symbol
                    AND m.effective_date = fund_nav_history.nav_date
            )
            AND NOT EXISTS (
                SELECT 1
                FROM scheduled_dca_occurrences AS d
                WHERE d.status = 'pending'
                    AND d.fund_symbol = fund_nav_history.fund_symbol
                    AND d.effective_date = fund_nav_history.nav_date
            )
        """,
        (cutoff.isoformat(),),
    )
    return cursor.rowcount


def _prune_scheduled_dca_occurrences(
    connection: sqlite3.Connection,
    *,
    cutoff: date,
) -> int:
    """Delete only terminal DCA occurrences older than the cutoff."""

    cursor = connection.execute(
        """
        DELETE FROM scheduled_dca_occurrences
        WHERE due_date < ?
            AND status IN ('skipped', 'applied', 'reconciled_by_sync')
        """,
        (cutoff.isoformat(),),
    )
    return cursor.rowcount


def _prune_active_position_evaluations(connection: sqlite3.Connection) -> int:
    """Keep only the latest successful evaluation for each active cycle."""

    cursor = connection.execute(
        """
        DELETE FROM position_profit_evaluations
        WHERE EXISTS (
                SELECT 1
                FROM position_cycles AS c
                WHERE c.id = position_profit_evaluations.position_cycle_id
                    AND c.ended_at IS NULL
            )
            AND nav_date < (
                SELECT MAX(latest.nav_date)
                FROM position_profit_evaluations AS latest
                WHERE latest.rule_id = position_profit_evaluations.rule_id
                    AND latest.position_cycle_id =
                        position_profit_evaluations.position_cycle_id
            )
        """
    )
    return cursor.rowcount


def _prune_ended_position_cycles(
    connection: sqlite3.Connection,
    *,
    cutoff: date,
) -> tuple[int, int, int]:
    """Remove old ended position cycles only when no fund work is pending."""

    thresholds_deleted = 0
    evaluations_deleted = 0
    cycles_deleted = 0
    cycles = connection.execute(
        """
        SELECT id, fund_symbol
        FROM position_cycles
        WHERE ended_at IS NOT NULL AND substr(ended_at, 1, 10) < ?
        ORDER BY id
        """,
        (cutoff.isoformat(),),
    ).fetchall()
    for cycle in cycles:
        cycle_id = int(cycle["id"])
        fund_symbol = str(cycle["fund_symbol"])
        if _position_cycle_has_blocking_work(connection, fund_symbol):
            continue
        thresholds_deleted += _delete_rows(
            connection,
            "DELETE FROM position_profit_thresholds WHERE position_cycle_id = ?",
            (cycle_id,),
        )
        evaluations_deleted += _delete_rows(
            connection,
            "DELETE FROM position_profit_evaluations WHERE position_cycle_id = ?",
            (cycle_id,),
        )
        cycles_deleted += _delete_rows(
            connection,
            "DELETE FROM position_cycles WHERE id = ? AND ended_at IS NOT NULL",
            (cycle_id,),
        )
    return thresholds_deleted, evaluations_deleted, cycles_deleted


def _position_cycle_has_blocking_work(
    connection: sqlite3.Connection,
    fund_symbol: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        WHERE EXISTS (
                SELECT 1 FROM manual_add_estimates
                WHERE fund_symbol = ? AND status = 'pending'
            )
            OR EXISTS (
                SELECT 1 FROM scheduled_dca_occurrences
                WHERE fund_symbol = ? AND status = 'pending'
            )
            OR EXISTS (
                SELECT 1
                FROM manual_add_actions AS a
                JOIN drawdown_cycles AS c ON c.id = a.cycle_id
                JOIN rules AS r ON r.id = c.rule_id
                WHERE a.reconciled_at IS NULL
                    AND json_extract(r.params_json, '$.investment_fund_symbol') = ?
            )
            OR EXISTS (
                SELECT 1 FROM position_snapshots
                WHERE fund_symbol = ? AND position_sync_required_since IS NOT NULL
            )
            OR EXISTS (
                SELECT 1 FROM fund_settings
                WHERE fund_symbol = ? AND position_sync_required_since IS NOT NULL
            )
        """,
        (fund_symbol, fund_symbol, fund_symbol, fund_symbol, fund_symbol),
    ).fetchone()
    return row is not None


def _prune_ended_drawdown_cycles(
    connection: sqlite3.Connection,
    *,
    cutoff: date,
) -> tuple[int, int, int, int, int]:
    """Remove old ended plan cycles in foreign-key child-first order."""

    actions_deleted = 0
    estimates_deleted = 0
    reminders_deleted = 0
    tiers_deleted = 0
    cycles_deleted = 0
    cycles = connection.execute(
        """
        SELECT id
        FROM drawdown_cycles
        WHERE end_date IS NOT NULL AND end_date < ?
        ORDER BY id
        """,
        (cutoff.isoformat(),),
    ).fetchall()
    for cycle in cycles:
        cycle_id = int(cycle["id"])
        if _drawdown_cycle_has_blocking_work(connection, cycle_id):
            continue
        actions_deleted += _delete_rows(
            connection,
            "DELETE FROM manual_add_actions WHERE cycle_id = ?",
            (cycle_id,),
        )
        estimates_deleted += _delete_rows(
            connection,
            "DELETE FROM manual_add_estimates WHERE cycle_id = ?",
            (cycle_id,),
        )
        reminders_deleted += _delete_rows(
            connection,
            "DELETE FROM drawdown_tier_reminder_states WHERE cycle_id = ?",
            (cycle_id,),
        )
        tiers_deleted += _delete_rows(
            connection,
            "DELETE FROM drawdown_tier_records WHERE cycle_id = ?",
            (cycle_id,),
        )
        # Keep the latest amount-change row per rule detached from pruned
        # cycles so monotonic amount_revision() never regresses on cascade.
        connection.execute(
            """
            UPDATE drawdown_plan_amount_changes
            SET cycle_id = NULL
            WHERE cycle_id = ?
              AND id IN (
                  SELECT MAX(id)
                  FROM drawdown_plan_amount_changes
                  GROUP BY rule_id
              )
            """,
            (cycle_id,),
        )
        cycles_deleted += _delete_rows(
            connection,
            "DELETE FROM drawdown_cycles WHERE id = ? AND end_date IS NOT NULL",
            (cycle_id,),
        )
    return (
        actions_deleted,
        estimates_deleted,
        reminders_deleted,
        tiers_deleted,
        cycles_deleted,
    )


def _drawdown_cycle_has_blocking_work(
    connection: sqlite3.Connection,
    cycle_id: int,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        WHERE EXISTS (
                SELECT 1 FROM manual_add_estimates
                WHERE cycle_id = ? AND status = 'pending'
            )
            OR EXISTS (
                SELECT 1 FROM manual_add_actions
                WHERE cycle_id = ? AND reconciled_at IS NULL
            )
        """,
        (cycle_id, cycle_id),
    ).fetchone()
    return row is not None


def _prune_alert_events(
    connection: sqlite3.Connection,
    *,
    today: date,
    cutoff: date,
) -> int:
    """Expire old events only when their keys cannot affect active dedupe."""
    from fund_alert_bot.db import (
        ALERT_NOTIFICATION_CANCELLED,
        ALERT_NOTIFICATION_SENT,
        NOTIFICATION_DELIVERY_CANCELLED,
        NOTIFICATION_DELIVERY_SENT,
    )

    deleted = 0
    rows = connection.execute(
        """
        SELECT id, rule_id, alert_key, title, payload_json
        FROM alert_events AS event
        WHERE substr(event.triggered_at, 1, 10) < ?
            AND event.notification_status IN (?, ?)
            AND NOT EXISTS (
                SELECT 1
                FROM notification_deliveries AS delivery
                WHERE delivery.event_id = event.id
                    AND delivery.status NOT IN (?, ?)
            )
        ORDER BY event.id
        """,
        (
            cutoff.isoformat(),
            ALERT_NOTIFICATION_SENT,
            ALERT_NOTIFICATION_CANCELLED,
            NOTIFICATION_DELIVERY_SENT,
            NOTIFICATION_DELIVERY_CANCELLED,
        ),
    ).fetchall()
    for row in rows:
        event_id = int(row["id"])
        if _alert_event_has_references(connection, event_id):
            continue
        if _alert_event_key_may_still_be_used(
            connection,
            row,
            today=today,
        ):
            continue
        deleted += _delete_rows(
            connection,
            "DELETE FROM alert_events WHERE id = ?",
            (event_id,),
        )
    return deleted


def _alert_event_has_references(
    connection: sqlite3.Connection,
    event_id: int,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        WHERE EXISTS (
                SELECT 1 FROM drawdown_tier_records
                WHERE alert_event_id = ?
            )
            OR EXISTS (
                SELECT 1 FROM position_profit_thresholds
                WHERE alert_event_id = ?
            )
            OR EXISTS (
                SELECT 1 FROM manual_add_estimates
                WHERE source_alert_event_id = ?
                    OR settlement_alert_event_id = ?
            )
            OR EXISTS (
                SELECT 1 FROM manual_add_actions
                WHERE source_alert_event_id = ?
            )
        """,
        (event_id, event_id, event_id, event_id, event_id),
    ).fetchone()
    return row is not None


def _alert_event_key_may_still_be_used(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    today: date,
) -> bool:
    from fund_alert_bot.db import is_auto_cost_profit_rule

    payload: dict[str, Any] = {}
    raw_payload = row["payload_json"]
    if raw_payload:
        try:
            loaded = json.loads(str(raw_payload))
            if isinstance(loaded, dict):
                payload = loaded
        except (TypeError, ValueError, json.JSONDecodeError):
            return True

    rule = connection.execute(
        "SELECT * FROM rules WHERE id = ?",
        (int(row["rule_id"]),),
    ).fetchone()
    rule_enabled = rule is not None and bool(rule["enabled"])
    rule_type = None if rule is None else str(rule["type"])
    title = str(row["title"])
    alert_key = str(row["alert_key"])
    phase = str(payload.get("phase", ""))

    if (
        rule is None
        and phase == ""
        and title
        in {
            "Drawdown reminder",
            "Price-Gain reminder",
        }
    ):
        return False

    if title == "Price-Gain reminder" and rule_enabled:
        if rule_type == "profit_reminder" and not is_auto_cost_profit_rule(rule):
            return True
        if phase == "position_profit":
            cycle_id = payload.get("position_cycle_id")
            if cycle_id is None:
                return True
            active = connection.execute(
                """
                SELECT 1 FROM position_cycles
                WHERE id = ? AND ended_at IS NULL
                """,
                (cycle_id,),
            ).fetchone()
            return active is not None

    if title == "Drawdown reminder" and rule_enabled:
        if rule_type not in {"drawdown_from_high", "drawdown"}:
            return False
        try:
            params = json.loads(str(rule["params_json"]))
            lookback = _positive_rule_int(params, "lookback_days")
            peak_value = payload.get("peak_date")
            if peak_value is None and ":peak:" in alert_key:
                peak_value = alert_key.split(":peak:", 1)[1].split(
                    ":threshold:",
                    1,
                )[0]
            peak_date = _parse_storage_date(peak_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return True
        return peak_date is None or peak_date >= today - timedelta(days=lookback)

    if alert_key.startswith("dca:"):
        due_date = _parse_storage_date(payload.get("due_date"))
        if due_date is None:
            parts = alert_key.split(":")
            if len(parts) == 3:
                due_date = _parse_storage_date(parts[2])
        return due_date is None or due_date >= today

    if phase == "position_profit":
        cycle_id = payload.get("position_cycle_id")
        if cycle_id is None:
            return True
        active = connection.execute(
            "SELECT 1 FROM position_cycles WHERE id = ? AND ended_at IS NULL",
            (cycle_id,),
        ).fetchone()
        return active is not None

    if phase == "manual_add_settled":
        estimate_id = payload.get("estimate_id")
        if estimate_id is None:
            return True
        estimate = connection.execute(
            "SELECT status FROM manual_add_estimates WHERE id = ?",
            (estimate_id,),
        ).fetchone()
        return estimate is not None and str(estimate["status"]) == "pending"

    if phase in {"before_close", "after_close", "fund_nav", "standard_recovery"}:
        return False
    if alert_key.startswith("data_unavailable:"):
        return False
    if alert_key.startswith("manual_add_settled:"):
        return False
    return True


def _positive_rule_int(
    params: Any,
    key: str,
    *,
    default: int | None = None,
) -> int:
    if not isinstance(params, dict):
        raise ValueError(f"rule params must contain {key}")
    raw = params.get(key, default)
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{key} must be positive")
    value = raw
    if value <= 0 or value > MAX_WINDOW_DAYS:
        raise ValueError(f"{key} must be between 1 and {MAX_WINDOW_DAYS}")
    return value


def _parse_storage_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _delete_rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
) -> int:
    cursor = connection.execute(sql, parameters)
    return max(cursor.rowcount, 0)
