"""SQLite storage helpers."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from fund_alert_bot.rules.drawdown_plan import format_plan_amount, format_plan_percent

ALERT_NOTIFICATION_PENDING = "pending"
ALERT_NOTIFICATION_SENT = "sent"
ALERT_NOTIFICATION_FAILED = "failed"
RETRYABLE_ALERT_NOTIFICATION_STATUSES = frozenset({ALERT_NOTIFICATION_FAILED})
SUPPRESSING_ALERT_NOTIFICATION_STATUSES = (
    ALERT_NOTIFICATION_PENDING,
    ALERT_NOTIFICATION_SENT,
)
STANDARD_NOTIFICATION_RECOVERY_MIGRATION_KEY = "standard_notification_recovery_v1"
STANDARD_NOTIFICATION_RECOVERY_NOTICE_TITLE = "Reminder recovery notice"
DEFAULT_RETENTION_DAYS = 400
NOTIFICATION_DELIVERY_PENDING = "pending"
NOTIFICATION_DELIVERY_SENDING = "sending"
NOTIFICATION_DELIVERY_SENT = "sent"
NOTIFICATION_DELIVERY_FAILED = "failed"
NOTIFICATION_DELIVERY_CLAIM_LEASE_SECONDS = 120


def connect(sqlite_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection and enable basic safety defaults."""
    path = Path(sqlite_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if path != Path(":memory:"):
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def open_connection(sqlite_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open and close a SQLite connection."""
    connection = connect(sqlite_path)
    try:
        yield connection
    finally:
        connection.close()


def init_db(connection: sqlite3.Connection) -> None:
    """Create storage tables if they do not already exist."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            params_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            alert_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT,
            triggered_at TEXT NOT NULL,
            notification_status TEXT NOT NULL DEFAULT 'pending',
            notification_attempted_at TEXT,
            notification_sent_at TEXT,
            notification_result_json TEXT
        );

        CREATE TABLE IF NOT EXISTS notification_deliveries (
            event_id INTEGER NOT NULL REFERENCES alert_events(id) ON DELETE CASCADE,
            target_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'sending', 'sent', 'failed')
            ),
            claim_token TEXT,
            claim_until TEXT,
            attempted_at TEXT,
            sent_at TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (event_id, target_key)
        );

        CREATE INDEX IF NOT EXISTS notification_deliveries_claim_lookup
        ON notification_deliveries(status, claim_until, event_id);

        CREATE TABLE IF NOT EXISTS notification_channels (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            config_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS drawdown_cycles (
            id INTEGER PRIMARY KEY,
            rule_id INTEGER NOT NULL REFERENCES rules(id),
            peak_date TEXT NOT NULL,
            initial_peak_price REAL NOT NULL,
            peak_price REAL NOT NULL,
            saw_below_peak INTEGER NOT NULL DEFAULT 0
                CHECK (saw_below_peak IN (0, 1)),
            last_evaluated_date TEXT NOT NULL,
            end_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS drawdown_cycles_one_active_rule
        ON drawdown_cycles(rule_id)
        WHERE end_date IS NULL;

        CREATE TABLE IF NOT EXISTS drawdown_tier_records (
            id INTEGER PRIMARY KEY,
            cycle_id INTEGER NOT NULL REFERENCES drawdown_cycles(id),
            tier_key TEXT NOT NULL,
            drawdown REAL NOT NULL,
            amount REAL NOT NULL,
            source TEXT NOT NULL CHECK (
                source IN ('close_confirmed', 'user_marked_added')
            ),
            data_date TEXT NOT NULL,
            alert_event_id INTEGER REFERENCES alert_events(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            UNIQUE(cycle_id, tier_key)
        );

        CREATE TABLE IF NOT EXISTS drawdown_tier_reminder_states (
            cycle_id INTEGER NOT NULL REFERENCES drawdown_cycles(id),
            tier_key TEXT NOT NULL,
            skipped_for_cycle INTEGER NOT NULL DEFAULT 0
                CHECK (skipped_for_cycle IN (0, 1)),
            snoozed_market_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (cycle_id, tier_key)
        );

        CREATE TABLE IF NOT EXISTS fund_settings (
            fund_symbol TEXT PRIMARY KEY,
            fee_mode TEXT CHECK (fee_mode IN ('rate', 'fixed')),
            fee_value REAL CHECK (fee_value >= 0),
            subscription_cutoff TEXT NOT NULL DEFAULT '15:00',
            position_sync_required_since TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (fee_mode IS NULL AND fee_value IS NULL)
                OR (fee_mode IS NOT NULL AND fee_value IS NOT NULL)
            )
        );

        CREATE TABLE IF NOT EXISTS position_snapshots (
            fund_symbol TEXT PRIMARY KEY,
            units REAL NOT NULL CHECK (units >= 0),
            average_unit_cost REAL NOT NULL CHECK (average_unit_cost >= 0),
            is_estimated INTEGER NOT NULL DEFAULT 0 CHECK (is_estimated IN (0, 1)),
            last_synced_at TEXT NOT NULL,
            estimates_since_sync INTEGER NOT NULL DEFAULT 0
                CHECK (estimates_since_sync >= 0),
            position_sync_required_since TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (units = 0 AND average_unit_cost = 0)
                OR (units > 0 AND average_unit_cost > 0)
            )
        );

        CREATE TABLE IF NOT EXISTS position_cycles (
            id INTEGER PRIMARY KEY,
            fund_symbol TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS position_cycles_one_active_fund
        ON position_cycles(fund_symbol)
        WHERE ended_at IS NULL;

        CREATE TABLE IF NOT EXISTS position_profit_thresholds (
            id INTEGER PRIMARY KEY,
            rule_id INTEGER NOT NULL REFERENCES rules(id),
            position_cycle_id INTEGER NOT NULL REFERENCES position_cycles(id),
            threshold_key TEXT NOT NULL,
            threshold REAL NOT NULL,
            alert_event_id INTEGER REFERENCES alert_events(id),
            created_at TEXT NOT NULL,
            UNIQUE(rule_id, position_cycle_id, threshold_key)
        );

        CREATE TABLE IF NOT EXISTS position_profit_evaluations (
            rule_id INTEGER NOT NULL REFERENCES rules(id),
            position_cycle_id INTEGER NOT NULL REFERENCES position_cycles(id),
            nav_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (rule_id, position_cycle_id, nav_date)
        );

        CREATE TABLE IF NOT EXISTS manual_add_estimates (
            id INTEGER PRIMARY KEY,
            rule_id INTEGER NOT NULL REFERENCES rules(id),
            cycle_id INTEGER NOT NULL REFERENCES drawdown_cycles(id),
            source_alert_event_id INTEGER NOT NULL REFERENCES alert_events(id),
            fund_symbol TEXT NOT NULL,
            tier_keys_json TEXT NOT NULL,
            gross_amount REAL NOT NULL CHECK (gross_amount > 0),
            fee_mode TEXT NOT NULL CHECK (fee_mode IN ('rate', 'fixed')),
            fee_value REAL NOT NULL CHECK (fee_value >= 0),
            action_at TEXT NOT NULL,
            action_date TEXT NOT NULL,
            cutoff_time TEXT NOT NULL,
            cutoff_choice TEXT NOT NULL CHECK (cutoff_choice IN ('before', 'after')),
            effective_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'applied', 'reconciled_by_sync')
            ),
            unit_nav REAL,
            nav_date TEXT,
            nav_source TEXT,
            net_amount REAL,
            added_units REAL,
            new_average_cost REAL,
            applied_at TEXT,
            settlement_alert_event_id INTEGER REFERENCES alert_events(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_alert_event_id, tier_keys_json)
        );

        CREATE TABLE IF NOT EXISTS manual_add_actions (
            cycle_id INTEGER NOT NULL REFERENCES drawdown_cycles(id),
            tier_key TEXT NOT NULL,
            source_alert_event_id INTEGER NOT NULL REFERENCES alert_events(id),
            estimate_id INTEGER REFERENCES manual_add_estimates(id),
            action_date TEXT NOT NULL,
            reconciled_at TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (cycle_id, tier_key)
        );

        CREATE TABLE IF NOT EXISTS scheduled_dca_occurrences (
            id INTEGER PRIMARY KEY,
            rule_id INTEGER NOT NULL REFERENCES rules(id),
            fund_symbol TEXT NOT NULL,
            due_date TEXT NOT NULL,
            gross_amount REAL NOT NULL CHECK (gross_amount > 0),
            fee_mode TEXT NOT NULL CHECK (fee_mode IN ('rate', 'fixed')),
            fee_value REAL NOT NULL CHECK (fee_value >= 0),
            holiday_policy TEXT NOT NULL CHECK (holiday_policy IN ('next', 'skip')),
            effective_date TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'skipped', 'applied', 'reconciled_by_sync')
            ),
            unit_nav REAL,
            nav_date TEXT,
            nav_source TEXT,
            net_amount REAL,
            added_units REAL,
            new_average_cost REAL,
            applied_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(rule_id, due_date)
        );

        CREATE TABLE IF NOT EXISTS market_daily_history (
            symbol TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            price_basis TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL NOT NULL,
            volume REAL,
            amount REAL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, asset_type, price_basis, date)
        );

        CREATE INDEX IF NOT EXISTS market_daily_history_lookup
        ON market_daily_history(symbol, asset_type, price_basis, date);

        CREATE TABLE IF NOT EXISTS fund_nav_history (
            fund_symbol TEXT NOT NULL,
            nav_date TEXT NOT NULL,
            unit_nav REAL NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (fund_symbol, nav_date)
        );

        CREATE INDEX IF NOT EXISTS fund_nav_history_lookup
        ON fund_nav_history(fund_symbol, nav_date);
        """
    )
    delivery_columns_added = _ensure_alert_event_delivery_columns(connection)
    _migrate_monotonic_ids(connection)
    _ensure_drawdown_cycle_columns(connection)
    _ensure_fund_settings_columns(connection)
    now = _utc_now_text()
    _ensure_standard_notification_recovery_migration(
        connection,
        delivery_columns_added=delivery_columns_added,
        now=now,
    )
    connection.execute(
        """
        INSERT INTO position_cycles (
            fund_symbol, started_at, created_at, updated_at
        )
        SELECT
            p.fund_symbol, p.last_synced_at, ?, ?
        FROM position_snapshots AS p
        WHERE p.units > 0 AND NOT EXISTS (
            SELECT 1 FROM position_cycles AS c
            WHERE c.fund_symbol = p.fund_symbol AND c.ended_at IS NULL
        )
        """,
        (now, now),
    )
    connection.commit()


def initialize_database(connection: sqlite3.Connection) -> None:
    """Backward-compatible alias for database initialization."""
    init_db(connection)


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
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, int]:
    """Prune bounded historical state without touching active work.

    The operation is one SQLite transaction.  A savepoint also makes the
    helper safe to call from a caller-owned transaction without committing
    unrelated work.
    """

    if isinstance(retention_days, bool) or not isinstance(retention_days, int):
        raise ValueError("retention_days must be a positive integer.")
    if retention_days <= 0:
        raise ValueError("retention_days must be a positive integer.")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date.")

    cutoff = today - timedelta(days=retention_days)
    counts = {table: 0 for table in _PRUNE_TABLES}
    connection.execute("SAVEPOINT prune_database")
    try:
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
        connection.execute("RELEASE SAVEPOINT prune_database")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT prune_database")
        connection.execute("RELEASE SAVEPOINT prune_database")
        raise
    return counts


def _prune_market_daily_history(
    connection: sqlite3.Connection,
    *,
    today: date,
) -> int:
    """Keep only the ranges required by currently enabled history rules."""

    # Backfill the one path-dependent bit before pruning an old active cycle's
    # price path. Future evaluations maintain it incrementally.
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
                    SELECT peak_date
                    FROM drawdown_cycles
                    WHERE rule_id = ? AND end_date IS NULL
                    """,
                    (int(rule["id"]),),
                ).fetchall()
            else:
                continue
        except (TypeError, ValueError, json.JSONDecodeError):
            # An invalid enabled rule cannot be evaluated safely.  Its
            # evaluator will report the configuration error; no cache window
            # is retained for a rule that has no valid window.
            continue
        previous = windows.get(key)
        if previous is None:
            windows[key] = (lower, set())
        elif lower < previous[0]:
            windows[key] = (lower, previous[1])

        if rule_type == "drawdown_plan":
            extra_dates = windows[key][1]
            for cycle in active_cycles:
                # ponytail: keep the peak fact, not an unbounded active-cycle
                # tail; the cycle row remains the durable business state.
                peak_date = _parse_storage_date(cycle["peak_date"])
                if peak_date is not None:
                    extra_dates.add(peak_date)

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

    deleted = 0
    rows = connection.execute(
        """
        SELECT id, rule_id, alert_key, title, payload_json
        FROM alert_events AS event
        WHERE substr(event.triggered_at, 1, 10) < ?
            AND event.notification_status = ?
            AND NOT EXISTS (
                SELECT 1
                FROM notification_deliveries AS delivery
                WHERE delivery.event_id = event.id
                    AND delivery.status != ?
            )
        ORDER BY event.id
        """,
        (
            cutoff.isoformat(),
            ALERT_NOTIFICATION_SENT,
            NOTIFICATION_DELIVERY_SENT,
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

    if title == "Price-Gain reminder" and rule_enabled:
        # Fixed-cost reminders intentionally use a once-per-cost key.  An
        # enabled rule must retain that key or the same threshold can fire
        # again after pruning.
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
    # Fail closed for alert types added after this retention policy: deleting
    # an unknown dedupe key can cause a duplicate investment reminder.
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
    if value <= 0:
        raise ValueError(f"{key} must be positive")
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


def upsert_market_history(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    asset_type: str,
    price_basis: str,
    rows: Sequence[dict[str, Any]],
) -> int:
    """Persist validated normalized daily rows and return the inserted count."""

    prepared: list[tuple[object, ...]] = []
    now = _utc_now_text()
    for row in rows:
        try:
            row_date = date.fromisoformat(str(row["date"])[:10]).isoformat()
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        values: list[float | None] = []
        for column in ("open", "high", "low", "volume", "amount"):
            try:
                value = float(row[column])
            except (KeyError, TypeError, ValueError):
                value = None
            values.append(value if value is not None and math.isfinite(value) else None)
        source = str(row.get("source", "")).strip()
        if not source:
            continue
        prepared.append(
            (
                symbol,
                asset_type,
                price_basis,
                row_date,
                *values[:3],
                close,
                *values[3:],
                source,
                now,
            )
        )
    if not prepared:
        return 0
    connection.executemany(
        """
        INSERT INTO market_daily_history (
            symbol, asset_type, price_basis, date,
            open, high, low, close, volume, amount, source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, asset_type, price_basis, date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            amount = excluded.amount,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        prepared,
    )
    connection.commit()
    return len(prepared)


def load_market_history(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    asset_type: str,
    price_basis: str,
    start_date: date,
    end_date: date,
) -> list[sqlite3.Row]:
    """Load persisted normalized daily rows in ascending date order."""

    return list(
        connection.execute(
            """
            SELECT date, open, high, low, close, volume, amount, source
            FROM market_daily_history
            WHERE symbol = ? AND asset_type = ? AND price_basis = ?
                AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            (
                symbol,
                asset_type,
                price_basis,
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        ).fetchall()
    )


def upsert_fund_nav(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    nav_date: date,
    unit_nav: float,
    source: str,
) -> None:
    """Persist one validated feeder-fund unit NAV."""

    value = float(unit_nav)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("unit_nav must be a positive finite number.")
    source_text = str(source).strip()
    if not source_text:
        raise ValueError("NAV source must not be empty.")
    connection.execute(
        """
        INSERT INTO fund_nav_history (
            fund_symbol, nav_date, unit_nav, source, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fund_symbol, nav_date) DO UPDATE SET
            unit_nav = excluded.unit_nav,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (fund_symbol, nav_date.isoformat(), value, source_text, _utc_now_text()),
    )
    connection.commit()


def get_cached_fund_nav(
    connection: sqlite3.Connection,
    fund_symbol: str,
    nav_date: date | None = None,
) -> sqlite3.Row | None:
    """Return one cached exact-date NAV, or the latest cached NAV."""

    if nav_date is None:
        return connection.execute(
            """
            SELECT fund_symbol, nav_date, unit_nav, source
            FROM fund_nav_history
            WHERE fund_symbol = ?
            ORDER BY nav_date DESC
            LIMIT 1
            """,
            (fund_symbol,),
        ).fetchone()
    return connection.execute(
        """
        SELECT fund_symbol, nav_date, unit_nav, source
        FROM fund_nav_history
        WHERE fund_symbol = ? AND nav_date = ?
        """,
        (fund_symbol, nav_date.isoformat()),
    ).fetchone()


def add_rule(
    connection: sqlite3.Connection,
    *,
    type: str,
    symbol: str,
    name: str,
    asset_type: str,
    params: Any,
    enabled: bool = True,
) -> int:
    """Insert an alert rule and return its database ID."""
    now = _utc_now_text()
    cursor = connection.execute(
        """
        INSERT INTO rules (
            type,
            symbol,
            name,
            asset_type,
            params_json,
            enabled,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            type,
            symbol,
            name,
            asset_type,
            _json_text(params),
            int(enabled),
            now,
            now,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def add_enhanced_dca_rule(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    name: str,
    weekday: str,
    amount: int | float,
    fee_mode: str,
    fee_value: float,
    holiday_policy: str,
) -> int:
    """Atomically validate shared settings and add one fixed weekly DCA rule."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        duplicate = connection.execute(
            """
            SELECT id
            FROM rules
            WHERE type = 'dca_reminder' AND asset_type = 'cn_open_fund'
                AND symbol = ? AND enabled = 1
                AND json_extract(params_json, '$.weekday') = ?
            LIMIT 1
            """,
            (fund_symbol, weekday),
        ).fetchone()
        if duplicate is not None:
            raise sqlite3.IntegrityError(
                "An enabled fixed DCA rule already uses this fund and weekday."
            )
        settings = get_fund_settings(connection, fund_symbol)
        if (
            settings is not None
            and settings["fee_mode"] is not None
            and (
                str(settings["fee_mode"]) != fee_mode
                or not math.isclose(float(settings["fee_value"]), fee_value)
            )
        ):
            raise sqlite3.IntegrityError(
                "This fund has a different fee; use /set_fund_fee first."
            )
        now = _utc_now_text()
        connection.execute(
            """
            INSERT INTO fund_settings (
                fund_symbol, fee_mode, fee_value, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fund_symbol) DO UPDATE SET
                fee_mode = COALESCE(fund_settings.fee_mode, excluded.fee_mode),
                fee_value = COALESCE(fund_settings.fee_value, excluded.fee_value),
                updated_at = excluded.updated_at
            """,
            (fund_symbol, fee_mode, fee_value, now, now),
        )
        cursor = connection.execute(
            """
            INSERT INTO rules (
                type, symbol, name, asset_type, params_json,
                enabled, created_at, updated_at
            ) VALUES ('dca_reminder', ?, ?, 'cn_open_fund', ?, 1, ?, ?)
            """,
            (
                fund_symbol,
                name,
                _json_text(
                    {
                        "weekday": weekday,
                        "amount": amount,
                        "holiday_policy": holiday_policy,
                    }
                ),
                now,
                now,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise


def update_dca_rule_amount(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    amount: int | float,
) -> sqlite3.Row:
    """Change one enabled DCA rule without modifying stored occurrences."""

    if isinstance(amount, bool) or not math.isfinite(float(amount)) or amount <= 0:
        raise ValueError("DCA amount must be a positive finite number.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM rules WHERE id = ? AND type = 'dca_reminder'",
            (rule_id,),
        ).fetchone()
        if row is None or not bool(row["enabled"]):
            raise sqlite3.IntegrityError("Enabled DCA rule was not found.")
        params = json.loads(str(row["params_json"]))
        if not isinstance(params, dict):
            raise sqlite3.IntegrityError("DCA rule parameters are invalid.")
        if str(row["asset_type"]) == "cn_open_fund":
            settings = get_fund_settings(connection, str(row["symbol"]))
            if (
                settings is not None
                and settings["fee_mode"] == "fixed"
                and float(settings["fee_value"]) >= float(amount)
            ):
                raise ValueError("Fixed fee must be lower than the DCA amount.")
        params["amount"] = amount
        connection.execute(
            "UPDATE rules SET params_json = ?, updated_at = ? WHERE id = ?",
            (_json_text(params), _utc_now_text(), rule_id),
        )
        updated = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if updated is None:
        raise RuntimeError("Updated DCA rule was not found.")
    return updated


def add_position_profit_rule(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    name: str,
    thresholds: Sequence[float],
) -> int:
    """Add the only enabled auto-cost Price-Gain rule for one feeder fund."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        duplicate = connection.execute(
            """
            SELECT id FROM rules
            WHERE type = 'profit_reminder' AND asset_type = 'cn_open_fund'
                AND symbol = ? AND enabled = 1
                AND json_valid(params_json)
                AND json_extract(params_json, '$.cost') = 'auto'
            LIMIT 1
            """,
            (fund_symbol,),
        ).fetchone()
        if duplicate is not None:
            raise sqlite3.IntegrityError(
                "An enabled auto-cost Price-Gain rule already uses this fund."
            )
        now = _utc_now_text()
        cursor = connection.execute(
            """
            INSERT INTO rules (
                type, symbol, name, asset_type, params_json,
                enabled, created_at, updated_at
            ) VALUES ('profit_reminder', ?, ?, 'cn_open_fund', ?, 1, ?, ?)
            """,
            (
                fund_symbol,
                name,
                _json_text({"cost": "auto", "thresholds": list(thresholds)}),
                now,
                now,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise


def add_drawdown_plan_rule(
    connection: sqlite3.Connection,
    *,
    reference_symbol: str,
    investment_fund_symbol: str,
    name: str,
    params: Any,
) -> int:
    """Insert one enabled one-to-one ETF/feeder-fund plan atomically."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        conflict = find_enabled_drawdown_plan_conflict(
            connection,
            reference_symbol=reference_symbol,
            investment_fund_symbol=investment_fund_symbol,
        )
        if conflict is not None:
            raise sqlite3.IntegrityError(
                "An enabled drawdown plan already uses the reference ETF or fund."
            )
        now = _utc_now_text()
        cursor = connection.execute(
            """
            INSERT INTO rules (
                type, symbol, name, asset_type, params_json,
                enabled, created_at, updated_at
            )
            VALUES ('drawdown_plan', ?, ?, 'cn_etf', ?, 1, ?, ?)
            """,
            (reference_symbol, name, _json_text(params), now, now),
        )
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise


def find_enabled_drawdown_plan_conflict(
    connection: sqlite3.Connection,
    *,
    reference_symbol: str,
    investment_fund_symbol: str,
) -> sqlite3.Row | None:
    """Return a plan already using either side of a proposed pair."""

    return connection.execute(
        """
        SELECT id, symbol, name, params_json
        FROM rules
        WHERE
            type = 'drawdown_plan'
            AND enabled = 1
            AND (
                symbol = ?
                OR json_extract(params_json, '$.investment_fund_symbol') = ?
            )
        ORDER BY id
        LIMIT 1
        """,
        (reference_symbol, investment_fund_symbol),
    ).fetchone()


def list_enabled_drawdown_plan_fund_symbols(
    connection: sqlite3.Connection,
) -> list[str]:
    """Return feeder-fund symbols owned by all enabled plan configurations."""

    return [
        str(row["fund_symbol"])
        for row in connection.execute(
            """
            SELECT json_extract(params_json, '$.investment_fund_symbol') AS fund_symbol
            FROM rules
            WHERE type = 'drawdown_plan' AND enabled = 1
            ORDER BY id
            """
        ).fetchall()
        if row["fund_symbol"] is not None
    ]


def list_rules(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all rules in insertion order."""
    return list(
        connection.execute(
            """
            SELECT
                id,
                type,
                symbol,
                name,
                asset_type,
                params_json,
                enabled,
                created_at,
                updated_at
            FROM rules
            ORDER BY id
            """
        ).fetchall()
    )


def list_enabled_rules(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return enabled rules in insertion order."""
    return list(
        connection.execute(
            """
            SELECT
                id,
                type,
                symbol,
                name,
                asset_type,
                params_json,
                enabled,
                created_at,
                updated_at
            FROM rules
            WHERE enabled = 1
            ORDER BY id
            """
        ).fetchall()
    )


def is_auto_cost_profit_rule(rule: Any) -> bool:
    """Return whether a rule is a valid position-linked Price-Gain rule."""

    if rule["type"] != "profit_reminder" or rule["asset_type"] != "cn_open_fund":
        return False
    try:
        params = json.loads(str(rule["params_json"]))
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(params, dict) and params.get("cost") == "auto"


def rule_removal_action(rule: Any) -> str:
    """Return the persistence-safe removal action for one rule."""

    if (
        rule["type"] == "drawdown_plan"
        or (rule["type"] == "dca_reminder" and rule["asset_type"] == "cn_open_fund")
        or is_auto_cost_profit_rule(rule)
    ):
        return "disable"
    return "delete"


def delete_rule(connection: sqlite3.Connection, rule_id: int) -> bool:
    """Delete a legacy rule or disable a stateful rule."""

    row = connection.execute(
        "SELECT type, asset_type, params_json FROM rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    if row is None:
        return False

    if rule_removal_action(row) == "disable":
        connection.execute(
            "UPDATE rules SET enabled = 0, updated_at = ? WHERE id = ?",
            (_utc_now_text(), rule_id),
        )
    else:
        connection.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    connection.commit()
    return True


def upsert_fund_fee(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    fee_mode: str,
    fee_value: float,
) -> sqlite3.Row:
    """Set the shared future-contribution fee for one feeder fund."""

    now = _utc_now_text()
    connection.execute(
        """
        INSERT INTO fund_settings (
            fund_symbol, fee_mode, fee_value, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fund_symbol) DO UPDATE SET
            fee_mode = excluded.fee_mode,
            fee_value = excluded.fee_value,
            updated_at = excluded.updated_at
        """,
        (fund_symbol, fee_mode, fee_value, now, now),
    )
    connection.commit()
    row = get_fund_settings(connection, fund_symbol)
    if row is None:
        raise RuntimeError("Fund settings upsert did not persist a row.")
    return row


def upsert_fund_cutoff(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    subscription_cutoff: str,
) -> sqlite3.Row:
    """Set the future manual-subscription cutoff for one feeder fund."""

    now = _utc_now_text()
    connection.execute(
        """
        INSERT INTO fund_settings (
            fund_symbol, subscription_cutoff, created_at, updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(fund_symbol) DO UPDATE SET
            subscription_cutoff = excluded.subscription_cutoff,
            updated_at = excluded.updated_at
        """,
        (fund_symbol, subscription_cutoff, now, now),
    )
    connection.commit()
    row = get_fund_settings(connection, fund_symbol)
    if row is None:
        raise RuntimeError("Fund settings upsert did not persist a row.")
    return row


def get_fund_settings(
    connection: sqlite3.Connection,
    fund_symbol: str,
) -> sqlite3.Row | None:
    """Return one feeder fund's shared settings."""

    return connection.execute(
        """
        SELECT
            fund_symbol,
            fee_mode,
            fee_value,
            subscription_cutoff,
            position_sync_required_since,
            created_at,
            updated_at
        FROM fund_settings
        WHERE fund_symbol = ?
        """,
        (fund_symbol,),
    ).fetchone()


def get_active_position_cycle(
    connection: sqlite3.Connection,
    fund_symbol: str,
) -> sqlite3.Row | None:
    """Return the continuous positive-position cycle for one fund."""

    return connection.execute(
        """
        SELECT * FROM position_cycles
        WHERE fund_symbol = ? AND ended_at IS NULL
        """,
        (fund_symbol,),
    ).fetchone()


def list_position_profit_threshold_keys(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    position_cycle_id: int,
) -> set[str]:
    """Return thresholds already recorded for one rule and position cycle."""

    return {
        str(row["threshold_key"])
        for row in connection.execute(
            """
            SELECT threshold_key FROM position_profit_thresholds
            WHERE rule_id = ? AND position_cycle_id = ?
            """,
            (rule_id, position_cycle_id),
        ).fetchall()
    }


def has_position_profit_evaluation(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    position_cycle_id: int,
    nav_date: str,
) -> bool:
    """Return whether this exact NAV date was already evaluated."""

    return (
        connection.execute(
            """
            SELECT 1 FROM position_profit_evaluations
            WHERE rule_id = ? AND position_cycle_id = ? AND nav_date = ?
            """,
            (rule_id, position_cycle_id, nav_date),
        ).fetchone()
        is not None
    )


def _position_profit_state_matches(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    position_cycle_id: int,
    units: float,
    average_unit_cost: float,
    is_estimated: bool,
) -> bool:
    rule = connection.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if rule is None or not bool(rule["enabled"]) or not is_auto_cost_profit_rule(rule):
        return False
    cycle = connection.execute(
        "SELECT fund_symbol, ended_at FROM position_cycles WHERE id = ?",
        (position_cycle_id,),
    ).fetchone()
    position = get_position_snapshot(connection, str(rule["symbol"]))
    settings = get_fund_settings(connection, str(rule["symbol"]))
    return bool(
        cycle is not None
        and cycle["ended_at"] is None
        and str(cycle["fund_symbol"]) == str(rule["symbol"])
        and position is not None
        and float(position["units"]) == units
        and float(position["average_unit_cost"]) == average_unit_cost
        and bool(position["is_estimated"]) == is_estimated
        and position["position_sync_required_since"] is None
        and (settings is None or settings["position_sync_required_since"] is None)
    )


def record_position_profit_evaluation(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    position_cycle_id: int,
    nav_date: str,
    position: Any,
) -> None:
    """Remember one successful no-alert evaluation."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        if not _position_profit_state_matches(
            connection,
            rule_id=rule_id,
            position_cycle_id=position_cycle_id,
            units=float(position["units"]),
            average_unit_cost=float(position["average_unit_cost"]),
            is_estimated=bool(position["is_estimated"]),
        ):
            raise sqlite3.IntegrityError("Position-linked gain state changed.")
        connection.execute(
            """
            INSERT OR IGNORE INTO position_profit_evaluations (
                rule_id, position_cycle_id, nav_date, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (rule_id, position_cycle_id, nav_date, _utc_now_text()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def list_position_profit_statuses(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return enabled auto-cost Price-Gain rules with current position state."""

    return list(
        connection.execute(
            """
            SELECT
                r.id AS rule_id,
                r.symbol AS fund_symbol,
                r.name,
                r.params_json,
                p.units,
                p.average_unit_cost,
                p.is_estimated,
                p.last_synced_at,
                p.position_sync_required_since AS snapshot_sync_required_since,
                fs.position_sync_required_since AS settings_sync_required_since,
                c.id AS position_cycle_id,
                COUNT(t.id) AS reached_thresholds
            FROM rules AS r
            LEFT JOIN position_snapshots AS p ON p.fund_symbol = r.symbol
            LEFT JOIN fund_settings AS fs ON fs.fund_symbol = r.symbol
            LEFT JOIN position_cycles AS c
                ON c.fund_symbol = r.symbol AND c.ended_at IS NULL
            LEFT JOIN position_profit_thresholds AS t
                ON t.rule_id = r.id AND t.position_cycle_id = c.id
            WHERE
                r.enabled = 1
                AND r.type = 'profit_reminder'
                AND r.asset_type = 'cn_open_fund'
                AND json_valid(r.params_json)
                AND json_extract(r.params_json, '$.cost') = 'auto'
            GROUP BY r.id, p.fund_symbol, c.id
            ORDER BY r.id
            """
        ).fetchall()
    )


def persist_position_profit_alert(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    position_cycle_id: int,
    alert: Any,
    thresholds: Sequence[tuple[str, float]],
    nav_date: str,
) -> int:
    """Atomically reserve one aggregate alert and its individual thresholds."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        payload = alert["payload"]
        if not _position_profit_state_matches(
            connection,
            rule_id=rule_id,
            position_cycle_id=position_cycle_id,
            units=float(payload["position_units"]),
            average_unit_cost=float(payload["average_unit_cost"]),
            is_estimated=payload["accuracy"] == "estimated",
        ):
            raise sqlite3.IntegrityError("Position-linked gain state changed.")
        existing = list_position_profit_threshold_keys(
            connection,
            rule_id=rule_id,
            position_cycle_id=position_cycle_id,
        )
        if any(key in existing for key, _value in thresholds):
            raise sqlite3.IntegrityError("Price-Gain threshold already recorded.")
        now = _utc_now_text()
        connection.execute(
            """
            INSERT INTO position_profit_evaluations (
                rule_id, position_cycle_id, nav_date, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (rule_id, position_cycle_id, nav_date, now),
        )
        cursor = connection.execute(
            """
            INSERT INTO alert_events (
                rule_id, alert_key, title, message, payload_json, triggered_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                str(alert["alert_key"]),
                str(alert["title"]),
                str(alert["message"]),
                _json_text(alert.get("payload")),
                now,
            ),
        )
        event_id = int(cursor.lastrowid)
        connection.executemany(
            """
            INSERT INTO position_profit_thresholds (
                rule_id, position_cycle_id, threshold_key,
                threshold, alert_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (rule_id, position_cycle_id, key, value, event_id, now)
                for key, value in thresholds
            ],
        )
        connection.commit()
        return event_id
    except Exception:
        connection.rollback()
        raise


def _maintain_position_cycle(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    new_units: float,
    changed_at: str,
) -> sqlite3.Row | None:
    active = get_active_position_cycle(connection, fund_symbol)
    now = _utc_now_text()
    if new_units == 0:
        if active is not None:
            connection.execute(
                """
                UPDATE position_cycles
                SET ended_at = ?, updated_at = ?
                WHERE id = ? AND ended_at IS NULL
                """,
                (changed_at, now, int(active["id"])),
            )
        return None
    if active is None:
        cursor = connection.execute(
            """
            INSERT INTO position_cycles (
                fund_symbol, started_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (fund_symbol, changed_at, now, now),
        )
        return connection.execute(
            "SELECT * FROM position_cycles WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    return active


def upsert_position_snapshot(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    units: float,
    average_unit_cost: float,
    synced_at: str | datetime | None = None,
) -> sqlite3.Row:
    """Replace one feeder-fund position with an exact platform snapshot."""

    sync_time = _timestamp_text(synced_at)
    now = _utc_now_text()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _maintain_position_cycle(
            connection,
            fund_symbol=fund_symbol,
            new_units=units,
            changed_at=sync_time,
        )
        connection.execute(
            """
            INSERT INTO position_snapshots (
                fund_symbol,
                units,
                average_unit_cost,
                is_estimated,
                last_synced_at,
                estimates_since_sync,
                position_sync_required_since,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 0, ?, 0, NULL, ?, ?)
            ON CONFLICT(fund_symbol) DO UPDATE SET
                units = excluded.units,
                average_unit_cost = excluded.average_unit_cost,
                is_estimated = 0,
                last_synced_at = excluded.last_synced_at,
                estimates_since_sync = 0,
                position_sync_required_since = NULL,
                updated_at = excluded.updated_at
            """,
            (fund_symbol, units, average_unit_cost, sync_time, now, now),
        )
        connection.execute(
            """
            UPDATE fund_settings
            SET position_sync_required_since = NULL, updated_at = ?
            WHERE fund_symbol = ?
            """,
            (now, fund_symbol),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    row = get_position_snapshot(connection, fund_symbol)
    if row is None:
        raise RuntimeError("Position snapshot upsert did not persist a row.")
    return row


def get_position_snapshot(
    connection: sqlite3.Connection,
    fund_symbol: str,
) -> sqlite3.Row | None:
    """Return the current snapshot for one feeder fund."""

    return connection.execute(
        """
        SELECT
            fund_symbol,
            units,
            average_unit_cost,
            is_estimated,
            last_synced_at,
            estimates_since_sync,
            position_sync_required_since,
            created_at,
            updated_at
        FROM position_snapshots
        WHERE fund_symbol = ?
        """,
        (fund_symbol,),
    ).fetchone()


def list_position_snapshots(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all current feeder-fund position snapshots."""

    return list(
        connection.execute(
            """
            SELECT
                p.fund_symbol,
                p.units,
                p.average_unit_cost,
                p.is_estimated,
                p.last_synced_at,
                p.estimates_since_sync,
                p.position_sync_required_since,
                fs.position_sync_required_since AS settings_sync_required_since,
                p.created_at,
                p.updated_at
            FROM position_snapshots AS p
            LEFT JOIN fund_settings AS fs ON fs.fund_symbol = p.fund_symbol
            ORDER BY p.fund_symbol
            """
        ).fetchall()
    )


def get_drawdown_plan_action_event(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    event_id: int | None = None,
    data_date: str | None = None,
    cycle_id: int | None = None,
    required_tier_keys: Sequence[str] = (),
) -> sqlite3.Row | None:
    """Return one enabled plan event eligible to start a manual-add action."""

    filters = ["e.rule_id = ?", "r.type = 'drawdown_plan'", "r.enabled = 1"]
    values: list[object] = [rule_id]
    if event_id is not None:
        filters.append("e.id = ?")
        values.append(event_id)
    if data_date is not None:
        filters.append("json_extract(e.payload_json, '$.data_date') = ?")
        values.append(data_date)
    if cycle_id is not None:
        filters.append("json_extract(e.payload_json, '$.cycle_id') = ?")
        values.append(cycle_id)
    for tier_key in required_tier_keys:
        filters.append(
            """
            (
                (
                    json_type(e.payload_json, '$.actionable_tiers') = 'array'
                    AND EXISTS (
                        SELECT 1
                        FROM json_each(e.payload_json, '$.actionable_tiers') AS tier
                        WHERE json_extract(tier.value, '$.key') = ?
                    )
                    AND (
                        COALESCE(
                            json_type(e.payload_json, '$.crossed_tiers'), ''
                        ) != 'array'
                        OR EXISTS (
                            SELECT 1
                            FROM json_each(e.payload_json, '$.crossed_tiers') AS tier
                            WHERE json_extract(tier.value, '$.key') = ?
                        )
                    )
                )
                OR (
                    COALESCE(
                        json_type(e.payload_json, '$.actionable_tiers'), ''
                    ) != 'array'
                    AND EXISTS (
                        SELECT 1
                        FROM json_each(e.payload_json, '$.crossed_tiers') AS tier
                        WHERE json_extract(tier.value, '$.key') = ?
                    )
                )
            )
            """
        )
        values.extend((tier_key, tier_key, tier_key))
    return connection.execute(
        f"""
        SELECT
            e.id,
            e.rule_id,
            e.payload_json,
            r.symbol,
            r.name,
            r.asset_type,
            r.params_json
        FROM alert_events AS e
        JOIN rules AS r ON r.id = e.rule_id
        WHERE {" AND ".join(filters)}
            AND (
                json_type(e.payload_json, '$.actionable_tiers') = 'array'
                OR json_type(e.payload_json, '$.crossed_tiers') = 'array'
            )
        ORDER BY e.id DESC
        LIMIT 1
        """,
        values,
    ).fetchone()


def list_manual_add_actions(
    connection: sqlite3.Connection,
    cycle_id: int,
) -> list[sqlite3.Row]:
    """Return user-recorded additions for one drawdown cycle."""

    return list(
        connection.execute(
            """
            SELECT
                cycle_id,
                tier_key,
                source_alert_event_id,
                estimate_id,
                action_date,
                created_at
            FROM manual_add_actions
            WHERE cycle_id = ?
            ORDER BY tier_key
            """,
            (cycle_id,),
        ).fetchall()
    )


def record_manual_addition(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    cycle_id: int,
    source_alert_event_id: int,
    fund_symbol: str,
    tiers: Sequence[Any],
    action_at: datetime,
    create_estimate: bool,
    action_date_override: date | None = None,
    cutoff_time: str | None = None,
    cutoff_choice: str | None = None,
    effective_date: str | None = None,
) -> tuple[int | None, tuple[str, ...]]:
    """Record selected tiers and optionally one fee-aware pending estimate."""

    if not tiers:
        raise ValueError("At least one drawdown tier must be selected.")
    if action_date_override is not None and create_estimate:
        raise ValueError("Historical additions cannot create position estimates.")
    if create_estimate and (
        cutoff_time is None
        or cutoff_choice not in {"before", "after"}
        or effective_date is None
    ):
        raise ValueError("A pending estimate requires a cutoff time, choice, and date.")

    action_text = _timestamp_text(action_at)
    action_date = (
        action_at.date() if action_date_override is None else action_date_override
    ).isoformat()
    now = _utc_now_text()
    connection.execute("BEGIN IMMEDIATE")
    try:
        rule = connection.execute(
            "SELECT enabled FROM rules WHERE id = ? AND type = 'drawdown_plan'",
            (rule_id,),
        ).fetchone()
        active = get_active_drawdown_cycle(connection, rule_id)
        event = connection.execute(
            "SELECT rule_id, payload_json FROM alert_events WHERE id = ?",
            (source_alert_event_id,),
        ).fetchone()
        if rule is None or not bool(rule["enabled"]):
            raise sqlite3.IntegrityError("Drawdown plan is no longer enabled.")
        if active is None or int(active["id"]) != cycle_id:
            raise sqlite3.IntegrityError("Drawdown plan cycle changed.")
        if event is None or int(event["rule_id"]) != rule_id:
            raise sqlite3.IntegrityError("Manual-add event does not match the plan.")
        payload = json.loads(str(event["payload_json"]))
        eligible_payload = payload.get(
            "actionable_tiers", payload.get("crossed_tiers", ())
        )
        eligible_tiers = {str(item["key"]): item for item in eligible_payload}
        selected_keys = tuple(str(tier.key) for tier in tiers)
        if (
            int(payload.get("cycle_id", -1)) != cycle_id
            or str(payload.get("data_date")) != action_date
            or str(payload.get("investment_fund_symbol")) != fund_symbol
            or len(set(selected_keys)) != len(selected_keys)
            or not set(selected_keys).issubset(eligible_tiers)
            or any(
                not math.isclose(
                    float(tier.drawdown),
                    float(eligible_tiers[str(tier.key)]["drawdown"]),
                )
                or not math.isclose(
                    float(tier.amount),
                    float(eligible_tiers[str(tier.key)]["amount"]),
                )
                for tier in tiers
            )
        ):
            raise sqlite3.IntegrityError("Manual-add event is no longer eligible.")

        existing_keys = {
            str(row["tier_key"])
            for row in connection.execute(
                "SELECT tier_key FROM manual_add_actions WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchall()
        }
        new_tiers = tuple(tier for tier in tiers if str(tier.key) not in existing_keys)
        if not new_tiers:
            connection.commit()
            return None, ()

        estimate_id: int | None = None
        if create_estimate:
            settings = get_fund_settings(connection, fund_symbol)
            position = get_position_snapshot(connection, fund_symbol)
            if settings is None or settings["fee_mode"] is None or position is None:
                raise sqlite3.IntegrityError("Plan setup is no longer READY.")
            if (
                settings["position_sync_required_since"] is not None
                or position["position_sync_required_since"] is not None
            ):
                raise sqlite3.IntegrityError(
                    "Position sync is required before another estimate."
                )
            gross_amount = sum(float(tier.amount) for tier in new_tiers)
            fee_mode = str(settings["fee_mode"])
            fee_value = float(settings["fee_value"])
            if fee_mode == "fixed" and fee_value >= gross_amount:
                raise ValueError("Fixed fee must be lower than the selected amount.")
            tier_keys_json = _json_text([str(tier.key) for tier in new_tiers])
            cursor = connection.execute(
                """
                INSERT INTO manual_add_estimates (
                    rule_id,
                    cycle_id,
                    source_alert_event_id,
                    fund_symbol,
                    tier_keys_json,
                    gross_amount,
                    fee_mode,
                    fee_value,
                    action_at,
                    action_date,
                    cutoff_time,
                    cutoff_choice,
                    effective_date,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    cycle_id,
                    source_alert_event_id,
                    fund_symbol,
                    tier_keys_json,
                    gross_amount,
                    fee_mode,
                    fee_value,
                    action_text,
                    action_date,
                    cutoff_time,
                    cutoff_choice,
                    effective_date,
                    now,
                    now,
                ),
            )
            estimate_id = int(cursor.lastrowid)
        else:
            connection.execute(
                """
                INSERT INTO fund_settings (
                    fund_symbol,
                    position_sync_required_since,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fund_symbol) DO UPDATE SET
                    position_sync_required_since = COALESCE(
                        fund_settings.position_sync_required_since,
                        excluded.position_sync_required_since
                    ),
                    updated_at = excluded.updated_at
                """,
                (fund_symbol, action_text, now, now),
            )
            connection.execute(
                """
                UPDATE position_snapshots
                SET position_sync_required_since = COALESCE(
                    position_sync_required_since, ?
                ), updated_at = ?
                WHERE fund_symbol = ?
                """,
                (action_text, now, fund_symbol),
            )

        connection.executemany(
            """
            INSERT INTO manual_add_actions (
                cycle_id,
                tier_key,
                source_alert_event_id,
                estimate_id,
                action_date,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    cycle_id,
                    str(tier.key),
                    source_alert_event_id,
                    estimate_id,
                    action_date,
                    now,
                )
                for tier in new_tiers
            ],
        )
        connection.executemany(
            """
            UPDATE drawdown_tier_reminder_states
            SET skipped_for_cycle = 0, updated_at = ?
            WHERE cycle_id = ? AND tier_key = ?
            """,
            [(now, cycle_id, str(tier.key)) for tier in new_tiers],
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO drawdown_tier_records (
                cycle_id,
                tier_key,
                drawdown,
                amount,
                source,
                data_date,
                alert_event_id,
                created_at
            )
            VALUES (?, ?, ?, ?, 'user_marked_added', ?, ?, ?)
            """,
            [
                (
                    cycle_id,
                    str(tier.key),
                    float(tier.drawdown),
                    float(tier.amount),
                    action_date,
                    source_alert_event_id,
                    now,
                )
                for tier in new_tiers
            ],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return estimate_id, tuple(str(tier.key) for tier in new_tiers)


def list_pending_manual_add_estimates(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str | None = None,
) -> list[sqlite3.Row]:
    """Return unapplied manual additions in stable order."""

    where = "WHERE status = 'pending'"
    values: tuple[object, ...] = ()
    if fund_symbol is not None:
        where += " AND fund_symbol = ?"
        values = (fund_symbol,)
    return list(
        connection.execute(
            f"""
            SELECT *
            FROM manual_add_estimates
            {where}
            ORDER BY id
            """,
            values,
        ).fetchall()
    )


def create_scheduled_dca_occurrence(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    fund_symbol: str,
    due_date: str,
    gross_amount: float,
    holiday_policy: str,
    effective_date: str | None,
    skipped: bool,
) -> sqlite3.Row:
    """Create one durable assumed DCA occurrence, preserving its first settings."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = get_scheduled_dca_occurrence(connection, rule_id, due_date)
        if existing is not None:
            connection.commit()
            return existing
        rule = connection.execute(
            """
            SELECT enabled, asset_type, symbol
            FROM rules WHERE id = ? AND type = 'dca_reminder'
            """,
            (rule_id,),
        ).fetchone()
        if (
            rule is None
            or not bool(rule["enabled"])
            or str(rule["asset_type"]) != "cn_open_fund"
            or str(rule["symbol"]) != fund_symbol
        ):
            raise sqlite3.IntegrityError("Fixed DCA rule is no longer enabled.")
        settings = get_fund_settings(connection, fund_symbol)
        if settings is None or settings["fee_mode"] is None:
            raise sqlite3.IntegrityError("Fixed DCA fund fee is missing.")
        fee_mode = str(settings["fee_mode"])
        fee_value = float(settings["fee_value"])
        if fee_mode == "fixed" and fee_value >= gross_amount:
            raise ValueError("Fixed fee must be lower than the DCA amount.")
        now = _utc_now_text()
        connection.execute(
            """
            INSERT INTO scheduled_dca_occurrences (
                rule_id, fund_symbol, due_date, gross_amount,
                fee_mode, fee_value, holiday_policy, effective_date,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                fund_symbol,
                due_date,
                gross_amount,
                fee_mode,
                fee_value,
                holiday_policy,
                effective_date,
                "skipped" if skipped else "pending",
                now,
                now,
            ),
        )
        row = get_scheduled_dca_occurrence(connection, rule_id, due_date)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if row is None:
        raise RuntimeError("Scheduled DCA occurrence was not persisted.")
    return row


def get_scheduled_dca_occurrence(
    connection: sqlite3.Connection,
    rule_id: int,
    due_date: str,
) -> sqlite3.Row | None:
    """Return one scheduled DCA occurrence."""

    return connection.execute(
        """
        SELECT * FROM scheduled_dca_occurrences
        WHERE rule_id = ? AND due_date = ?
        """,
        (rule_id, due_date),
    ).fetchone()


def list_pending_scheduled_dca_occurrences(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str | None = None,
) -> list[sqlite3.Row]:
    """Return pending fixed DCA occurrences in stable order."""

    where = "WHERE status = 'pending'"
    values: tuple[object, ...] = ()
    if fund_symbol is not None:
        where += " AND fund_symbol = ?"
        values = (fund_symbol,)
    return list(
        connection.execute(
            f"""
            SELECT * FROM scheduled_dca_occurrences
            {where}
            ORDER BY due_date, id
            """,
            values,
        ).fetchall()
    )


def list_enhanced_dca_statuses(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return enabled fixed DCA rules with their latest occurrence and position."""

    return list(
        connection.execute(
            """
            SELECT
                r.id AS rule_id,
                r.symbol AS fund_symbol,
                r.name,
                r.params_json,
                o.due_date,
                o.effective_date,
                o.status,
                o.gross_amount,
                o.added_units,
                o.nav_date,
                p.is_estimated,
                p.last_synced_at,
                p.estimates_since_sync
            FROM rules AS r
            LEFT JOIN scheduled_dca_occurrences AS o ON o.id = (
                SELECT latest.id
                FROM scheduled_dca_occurrences AS latest
                WHERE latest.rule_id = r.id
                ORDER BY latest.due_date DESC, latest.id DESC
                LIMIT 1
            )
            LEFT JOIN position_snapshots AS p ON p.fund_symbol = r.symbol
            WHERE r.type = 'dca_reminder'
                AND r.asset_type = 'cn_open_fund'
                AND r.enabled = 1
            ORDER BY r.id
            """
        ).fetchall()
    )


def set_scheduled_dca_effective_date(
    connection: sqlite3.Connection,
    *,
    occurrence_id: int,
    effective_date: str,
) -> None:
    """Persist the first confirmed open date for a pending occurrence."""

    connection.execute(
        """
        UPDATE scheduled_dca_occurrences
        SET effective_date = COALESCE(effective_date, ?), updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (effective_date, _utc_now_text(), occurrence_id),
    )
    connection.commit()


def skip_scheduled_dca_occurrence(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    due_date: str,
) -> str:
    """Skip only a still-pending occurrence and return its resulting state."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = get_scheduled_dca_occurrence(connection, rule_id, due_date)
        if row is None:
            connection.commit()
            return "missing"
        status = str(row["status"])
        if status == "pending":
            connection.execute(
                """
                UPDATE scheduled_dca_occurrences
                SET status = 'skipped', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (_utc_now_text(), int(row["id"])),
            )
            status = "skipped"
        connection.commit()
        return status
    except Exception:
        connection.rollback()
        raise


def apply_scheduled_dca_occurrence(
    connection: sqlite3.Connection,
    *,
    occurrence_id: int,
    nav: Any,
) -> bool:
    """Apply one exact-date fixed DCA estimate to its position at most once."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        occurrence = connection.execute(
            "SELECT * FROM scheduled_dca_occurrences WHERE id = ?",
            (occurrence_id,),
        ).fetchone()
        if occurrence is None or str(occurrence["status"]) != "pending":
            connection.commit()
            return False
        nav_value = float(nav.value)
        if (
            str(nav.symbol) != str(occurrence["fund_symbol"])
            or nav.date.isoformat() != str(occurrence["effective_date"])
            or str(nav.source) != "akshare_eastmoney"
            or not math.isfinite(nav_value)
            or nav_value <= 0
        ):
            raise ValueError("Fund NAV does not match the scheduled DCA occurrence.")
        position = get_position_snapshot(connection, str(occurrence["fund_symbol"]))
        if position is None:
            raise sqlite3.IntegrityError("Position snapshot is missing.")
        settings = get_fund_settings(connection, str(occurrence["fund_symbol"]))
        if (
            settings is not None
            and settings["position_sync_required_since"] is not None
        ) or position["position_sync_required_since"] is not None:
            raise sqlite3.IntegrityError(
                "Position sync is required before applying pending estimates."
            )
        gross_amount = float(occurrence["gross_amount"])
        fee_value = float(occurrence["fee_value"])
        net_amount = (
            gross_amount / (1 + fee_value)
            if str(occurrence["fee_mode"]) == "rate"
            else gross_amount - fee_value
        )
        if net_amount <= 0:
            raise ValueError("Subscription fee leaves no investable amount.")
        added_units = net_amount / nav_value
        old_units = float(position["units"])
        new_units = old_units + added_units
        new_average_cost = (
            old_units * float(position["average_unit_cost"]) + gross_amount
        ) / new_units
        now = _utc_now_text()
        _maintain_position_cycle(
            connection,
            fund_symbol=str(occurrence["fund_symbol"]),
            new_units=new_units,
            changed_at=now,
        )
        connection.execute(
            """
            UPDATE position_snapshots
            SET units = ?, average_unit_cost = ?, is_estimated = 1,
                estimates_since_sync = estimates_since_sync + 1, updated_at = ?
            WHERE fund_symbol = ?
            """,
            (new_units, new_average_cost, now, str(occurrence["fund_symbol"])),
        )
        connection.execute(
            """
            UPDATE scheduled_dca_occurrences
            SET status = 'applied', unit_nav = ?, nav_date = ?, nav_source = ?,
                net_amount = ?, added_units = ?, new_average_cost = ?,
                applied_at = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                nav_value,
                nav.date.isoformat(),
                str(nav.source),
                net_amount,
                added_units,
                new_average_cost,
                now,
                now,
                occurrence_id,
            ),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def list_pending_position_items(
    connection: sqlite3.Connection,
    fund_symbol: str,
) -> list[dict[str, object]]:
    """Return pending estimated and unestimated additions for sync preview."""

    items = [
        {
            "key": f"estimate:{row['id']}",
            "kind": "manual estimate",
            "date": str(row["action_date"]),
            "amount": float(row["gross_amount"]),
        }
        for row in list_pending_manual_add_estimates(
            connection,
            fund_symbol=fund_symbol,
        )
    ]
    items.extend(
        {
            "key": f"dca:{row['id']}",
            "kind": "scheduled DCA estimate",
            "date": str(row["due_date"]),
            "amount": float(row["gross_amount"]),
        }
        for row in list_pending_scheduled_dca_occurrences(
            connection,
            fund_symbol=fund_symbol,
        )
    )
    items.extend(
        {
            "key": f"action:{row['cycle_id']}:{row['tier_key']}",
            "kind": "manual add requiring sync",
            "date": str(row["action_date"]),
            "amount": float(row["amount"]),
        }
        for row in connection.execute(
            """
            SELECT
                a.cycle_id,
                a.tier_key,
                a.action_date,
                t.amount
            FROM manual_add_actions AS a
            JOIN drawdown_cycles AS c ON c.id = a.cycle_id
            JOIN rules AS r ON r.id = c.rule_id
            JOIN drawdown_tier_records AS t
                ON t.cycle_id = a.cycle_id AND t.tier_key = a.tier_key
            WHERE
                json_extract(r.params_json, '$.investment_fund_symbol') = ?
                AND a.estimate_id IS NULL
                AND a.reconciled_at IS NULL
            ORDER BY a.action_date, a.cycle_id, a.tier_key
            """,
            (fund_symbol,),
        ).fetchall()
    )
    return sorted(items, key=lambda item: str(item["key"]))


def reconcile_position_snapshot(
    connection: sqlite3.Connection,
    *,
    fund_symbol: str,
    units: float,
    average_unit_cost: float,
    expected_item_keys: Sequence[str],
    all_included: bool,
    synced_at: datetime,
) -> sqlite3.Row:
    """Apply an exact snapshot after verifying the displayed pending set."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        current_keys = tuple(
            str(item["key"])
            for item in list_pending_position_items(connection, fund_symbol)
        )
        if current_keys != tuple(expected_item_keys):
            raise sqlite3.IntegrityError(
                "Pending additions changed; rerun /sync_position."
            )
        sync_time = _timestamp_text(synced_at)
        now = _utc_now_text()
        _maintain_position_cycle(
            connection,
            fund_symbol=fund_symbol,
            new_units=units,
            changed_at=sync_time,
        )
        connection.execute(
            """
            INSERT INTO position_snapshots (
                fund_symbol,
                units,
                average_unit_cost,
                is_estimated,
                last_synced_at,
                estimates_since_sync,
                position_sync_required_since,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 0, ?, 0, NULL, ?, ?)
            ON CONFLICT(fund_symbol) DO UPDATE SET
                units = excluded.units,
                average_unit_cost = excluded.average_unit_cost,
                is_estimated = 0,
                last_synced_at = excluded.last_synced_at,
                estimates_since_sync = 0,
                position_sync_required_since = NULL,
                updated_at = excluded.updated_at
            """,
            (fund_symbol, units, average_unit_cost, sync_time, now, now),
        )
        if all_included:
            estimate_ids = [
                int(key.split(":", 1)[1])
                for key in current_keys
                if key.startswith("estimate:")
            ]
            action_keys = [
                key.split(":", 2)[1:]
                for key in current_keys
                if key.startswith("action:")
            ]
            dca_ids = [
                int(key.split(":", 1)[1])
                for key in current_keys
                if key.startswith("dca:")
            ]
            if estimate_ids:
                connection.executemany(
                    """
                    UPDATE manual_add_estimates
                    SET status = 'reconciled_by_sync', updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    [(now, estimate_id) for estimate_id in estimate_ids],
                )
            if action_keys:
                connection.executemany(
                    """
                    UPDATE manual_add_actions
                    SET reconciled_at = ?
                    WHERE cycle_id = ? AND tier_key = ? AND reconciled_at IS NULL
                    """,
                    [
                        (now, int(cycle_id), tier_key)
                        for cycle_id, tier_key in action_keys
                    ],
                )
            if dca_ids:
                connection.executemany(
                    """
                    UPDATE scheduled_dca_occurrences
                    SET status = 'reconciled_by_sync', updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    [(now, occurrence_id) for occurrence_id in dca_ids],
                )
        unresolved = connection.execute(
            """
            SELECT 1
            FROM manual_add_actions AS a
            JOIN drawdown_cycles AS c ON c.id = a.cycle_id
            JOIN rules AS r ON r.id = c.rule_id
            WHERE
                json_extract(r.params_json, '$.investment_fund_symbol') = ?
                AND a.estimate_id IS NULL
                AND a.reconciled_at IS NULL
            LIMIT 1
            """,
            (fund_symbol,),
        ).fetchone()
        connection.execute(
            """
            UPDATE fund_settings
            SET position_sync_required_since = ?, updated_at = ?
            WHERE fund_symbol = ?
            """,
            (None if unresolved is None else sync_time, now, fund_symbol),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    row = get_position_snapshot(connection, fund_symbol)
    if row is None:
        raise RuntimeError("Position snapshot reconciliation did not persist.")
    return row


def apply_manual_add_estimate(
    connection: sqlite3.Connection,
    *,
    estimate_id: int,
    nav: Any,
) -> dict[str, object] | None:
    """Atomically apply one exact-date NAV estimate and reserve its notice."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        occurrence = connection.execute(
            "SELECT * FROM manual_add_estimates WHERE id = ?",
            (estimate_id,),
        ).fetchone()
        if occurrence is None or str(occurrence["status"]) != "pending":
            connection.commit()
            return None
        nav_value = float(nav.value)
        if (
            str(nav.symbol) != str(occurrence["fund_symbol"])
            or nav.date.isoformat() != str(occurrence["effective_date"])
            or str(nav.source) != "akshare_eastmoney"
            or not math.isfinite(nav_value)
            or nav_value <= 0
        ):
            raise ValueError("Fund NAV does not match the pending estimate.")
        position = get_position_snapshot(connection, str(occurrence["fund_symbol"]))
        if position is None:
            raise sqlite3.IntegrityError("Position snapshot is missing.")
        settings = get_fund_settings(connection, str(occurrence["fund_symbol"]))
        if (
            settings is not None
            and settings["position_sync_required_since"] is not None
        ) or position["position_sync_required_since"] is not None:
            raise sqlite3.IntegrityError(
                "Position sync is required before applying pending estimates."
            )
        gross_amount = float(occurrence["gross_amount"])
        fee_value = float(occurrence["fee_value"])
        net_amount = (
            gross_amount / (1 + fee_value)
            if str(occurrence["fee_mode"]) == "rate"
            else gross_amount - fee_value
        )
        if net_amount <= 0:
            raise ValueError("Subscription fee leaves no investable amount.")
        added_units = net_amount / nav_value
        old_units = float(position["units"])
        new_units = old_units + added_units
        new_average_cost = (
            old_units * float(position["average_unit_cost"]) + gross_amount
        ) / new_units
        now = _utc_now_text()
        _maintain_position_cycle(
            connection,
            fund_symbol=str(occurrence["fund_symbol"]),
            new_units=new_units,
            changed_at=now,
        )
        connection.execute(
            """
            UPDATE position_snapshots
            SET
                units = ?,
                average_unit_cost = ?,
                is_estimated = 1,
                estimates_since_sync = estimates_since_sync + 1,
                updated_at = ?
            WHERE fund_symbol = ?
            """,
            (
                new_units,
                new_average_cost,
                now,
                str(occurrence["fund_symbol"]),
            ),
        )
        tier_keys = json.loads(str(occurrence["tier_keys_json"]))
        fee = (
            f"rate:{format_plan_percent(fee_value)}"
            if str(occurrence["fee_mode"]) == "rate"
            else f"fixed:{format_plan_amount(fee_value)}"
        )
        message = "\n".join(
            (
                "✅ Manual addition estimate updated",
                "",
                f"Fund: {occurrence['fund_symbol']}",
                "Configured tiers: "
                + ", ".join("-" + format_plan_percent(str(key)) for key in tier_keys),
                f"Gross amount: {format_plan_amount(gross_amount)}",
                f"Captured subscription fee: {fee}",
                f"Effective date: {occurrence['effective_date']}",
                f"Unit NAV: {nav_value:.12g} on {nav.date}",
                f"NAV source: {nav.source}",
                f"Estimated added units: {added_units:.6f}",
                f"New estimated average cost: {new_average_cost:.6f}",
                "",
                "Estimated only; not yet synchronized with the fund platform.",
                "No trade has been placed or verified.",
            )
        )
        payload = {
            "phase": "manual_add_settled",
            "estimate_id": estimate_id,
            "rule_id": int(occurrence["rule_id"]),
            "cycle_id": int(occurrence["cycle_id"]),
            "fund_symbol": str(occurrence["fund_symbol"]),
            "tier_keys": tier_keys,
            "gross_amount": gross_amount,
            "fee_mode": str(occurrence["fee_mode"]),
            "fee_value": fee_value,
            "action_date": str(occurrence["action_date"]),
            "effective_date": str(occurrence["effective_date"]),
            "nav_date": nav.date.isoformat(),
            "unit_nav": nav_value,
            "nav_source": str(nav.source),
            "net_amount": net_amount,
            "added_units": added_units,
            "new_average_cost": new_average_cost,
            "estimated": True,
        }
        cursor = connection.execute(
            """
            INSERT INTO alert_events (
                rule_id,
                alert_key,
                title,
                message,
                payload_json,
                triggered_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(occurrence["rule_id"]),
                f"manual_add_settled:{estimate_id}",
                "Manual addition estimate updated",
                message,
                _json_text(payload),
                now,
            ),
        )
        event_id = int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE manual_add_estimates
            SET
                status = 'applied',
                unit_nav = ?,
                nav_date = ?,
                nav_source = ?,
                net_amount = ?,
                added_units = ?,
                new_average_cost = ?,
                applied_at = ?,
                settlement_alert_event_id = ?,
                updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                nav_value,
                nav.date.isoformat(),
                str(nav.source),
                net_amount,
                added_units,
                new_average_cost,
                now,
                event_id,
                now,
                estimate_id,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "event_id": event_id,
        "title": "Manual addition estimate updated",
        "message": message,
    }


def alert_exists(connection: sqlite3.Connection, alert_key: str) -> bool:
    """Return whether a non-failed alert event with the key already exists."""
    row = connection.execute(
        """
        SELECT 1
        FROM alert_events
        WHERE alert_key = ?
            AND notification_status IN (?, ?)
        LIMIT 1
        """,
        (
            alert_key,
            *SUPPRESSING_ALERT_NOTIFICATION_STATUSES,
        ),
    ).fetchone()
    return row is not None


def add_alert_event(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    alert_key: str,
    title: str,
    message: str,
    payload: Any | None = None,
    triggered_at: str | datetime | None = None,
) -> int:
    """Insert an alert event and return its database ID."""
    cursor = connection.execute(
        """
        INSERT INTO alert_events (
            rule_id,
            alert_key,
            title,
            message,
            payload_json,
            triggered_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            rule_id,
            alert_key,
            title,
            message,
            None if payload is None else _json_text(payload),
            _timestamp_text(triggered_at),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def add_drawdown_plan_pre_alert_event(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    alert: Any,
) -> int:
    """Reserve one expiring pre-alert only while its plan remains enabled."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        rule = connection.execute(
            "SELECT type, enabled FROM rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if (
            rule is None
            or str(rule["type"]) != "drawdown_plan"
            or not bool(rule["enabled"])
        ):
            raise sqlite3.IntegrityError("Drawdown plan is no longer enabled.")
        cursor = connection.execute(
            """
            INSERT INTO alert_events (
                rule_id,
                alert_key,
                title,
                message,
                payload_json,
                triggered_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                str(alert["alert_key"]),
                str(alert["title"]),
                str(alert["message"]),
                _json_text(alert.get("payload")),
                _utc_now_text(),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return int(cursor.lastrowid)


def reserve_alert_event(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    alert_key: str,
    title: str,
    message: str,
    payload: Any | None = None,
    triggered_at: str | datetime | None = None,
) -> int:
    """Create or re-reserve a retryable alert event for notification delivery."""

    try:
        return add_alert_event(
            connection,
            rule_id=rule_id,
            alert_key=alert_key,
            title=title,
            message=message,
            payload=payload,
            triggered_at=triggered_at,
        )
    except sqlite3.IntegrityError:
        row = connection.execute(
            """
            SELECT id, notification_status
            FROM alert_events
            WHERE alert_key = ?
            """,
            (alert_key,),
        ).fetchone()
        if (
            row is None
            or row["notification_status"] not in RETRYABLE_ALERT_NOTIFICATION_STATUSES
        ):
            raise

        event_id = int(row["id"])
        connection.execute(
            """
            UPDATE alert_events
            SET
                rule_id = ?,
                title = ?,
                message = ?,
                payload_json = ?,
                triggered_at = ?,
                notification_status = ?,
                notification_sent_at = NULL,
                notification_result_json = NULL
            WHERE id = ?
            """,
            (
                rule_id,
                title,
                message,
                None if payload is None else _json_text(payload),
                _timestamp_text(triggered_at),
                ALERT_NOTIFICATION_PENDING,
                event_id,
            ),
        )
        connection.commit()
        return event_id


def record_alert_notification_result(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    results: Sequence[Any],
) -> None:
    """Record channel delivery results for an alert event."""

    result_payload = [_notification_result_payload(result) for result in results]
    delivered = any(bool(result["success"]) for result in result_payload)
    now = _utc_now_text()
    connection.execute(
        """
        UPDATE alert_events
        SET
            notification_status = ?,
            notification_attempted_at = ?,
            notification_sent_at = ?,
            notification_result_json = ?
        WHERE id = ?
        """,
        (
            ALERT_NOTIFICATION_SENT if delivered else ALERT_NOTIFICATION_FAILED,
            now,
            now if delivered else None,
            _json_text(result_payload),
            event_id,
        ),
    )
    connection.commit()


@dataclass(frozen=True, slots=True)
class NotificationDeliveryClaim:
    """One leased event/notification-target delivery attempt."""

    event_id: int
    target_key: str
    channel: str
    claim_token: str


def ensure_notification_delivery_targets(
    connection: sqlite3.Connection,
    *,
    event_ids: Sequence[int],
    targets: Sequence[tuple[str, str]],
) -> None:
    """Freeze the currently configured targets for events without targets."""

    normalized_event_ids = tuple(dict.fromkeys(int(event_id) for event_id in event_ids))
    normalized_targets = tuple(
        (str(target_key), str(channel)) for target_key, channel in targets
    )
    if not normalized_event_ids:
        return
    if any(not target_key or not channel for target_key, channel in normalized_targets):
        raise ValueError("Notification targets must have non-empty keys and channels.")
    if len({target_key for target_key, _ in normalized_targets}) != len(
        normalized_targets
    ):
        raise ValueError("Notification target keys must be unique.")

    connection.execute("BEGIN IMMEDIATE")
    try:
        now = _utc_now_text()
        for event_id in normalized_event_ids:
            event = connection.execute(
                "SELECT notification_status FROM alert_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if (
                event is None
                or str(event["notification_status"]) == ALERT_NOTIFICATION_SENT
            ):
                continue
            if (
                connection.execute(
                    """
                SELECT 1
                FROM notification_deliveries
                WHERE event_id = ?
                LIMIT 1
                """,
                    (event_id,),
                ).fetchone()
                is not None
            ):
                continue
            connection.executemany(
                """
                INSERT INTO notification_deliveries (
                    event_id, target_key, channel, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (event_id, target_key, channel, now, now)
                    for target_key, channel in normalized_targets
                ],
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def claim_notification_deliveries(
    connection: sqlite3.Connection,
    *,
    event_ids: Sequence[int],
    target_keys: Sequence[str] | None = None,
    lease_seconds: int = NOTIFICATION_DELIVERY_CLAIM_LEASE_SECONDS,
) -> list[NotificationDeliveryClaim]:
    """Atomically lease retryable target deliveries for one dispatcher."""

    normalized_event_ids = tuple(dict.fromkeys(int(event_id) for event_id in event_ids))
    if not normalized_event_ids:
        return []
    if isinstance(lease_seconds, bool) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive.")
    normalized_target_keys = (
        None
        if target_keys is None
        else tuple(dict.fromkeys(str(target_key) for target_key in target_keys))
    )

    connection.execute("BEGIN IMMEDIATE")
    try:
        now = _utc_now_text()
        claim_until = (
            (datetime.now(UTC) + timedelta(seconds=lease_seconds))
            .replace(microsecond=0)
            .isoformat()
        )
        event_placeholders = ", ".join("?" for _ in normalized_event_ids)
        params: list[object] = [
            *normalized_event_ids,
            NOTIFICATION_DELIVERY_PENDING,
            NOTIFICATION_DELIVERY_FAILED,
            NOTIFICATION_DELIVERY_SENDING,
            now,
        ]
        target_filter = ""
        if normalized_target_keys:
            target_placeholders = ", ".join("?" for _ in normalized_target_keys)
            target_filter = f"AND target_key IN ({target_placeholders})"
            params.extend(normalized_target_keys)
        rows = connection.execute(
            f"""
            SELECT event_id, target_key, channel
            FROM notification_deliveries
            WHERE event_id IN ({event_placeholders})
                AND (
                    status IN (?, ?)
                    OR (status = ? AND (claim_until IS NULL OR claim_until <= ?))
                )
                {target_filter}
            ORDER BY event_id, target_key
            """,
            params,
        ).fetchall()
        claims: list[NotificationDeliveryClaim] = []
        for row in rows:
            token = uuid4().hex
            connection.execute(
                """
                UPDATE notification_deliveries
                SET
                    status = ?,
                    claim_token = ?,
                    claim_until = ?,
                    updated_at = ?
                WHERE event_id = ? AND target_key = ?
                """,
                (
                    NOTIFICATION_DELIVERY_SENDING,
                    token,
                    claim_until,
                    now,
                    int(row["event_id"]),
                    str(row["target_key"]),
                ),
            )
            claims.append(
                NotificationDeliveryClaim(
                    event_id=int(row["event_id"]),
                    target_key=str(row["target_key"]),
                    channel=str(row["channel"]),
                    claim_token=token,
                )
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return claims


def complete_notification_delivery(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    target_key: str,
    claim_token: str,
    result: Any,
) -> bool:
    """Complete a leased target delivery, ignoring stale workers."""

    result_payload = _notification_result_payload(result)
    success = bool(result_payload["success"])
    now = _utc_now_text()
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            """
            UPDATE notification_deliveries
            SET
                status = ?,
                claim_token = NULL,
                claim_until = NULL,
                attempted_at = ?,
                sent_at = CASE WHEN ? THEN ? ELSE NULL END,
                result_json = ?,
                updated_at = ?
            WHERE event_id = ?
                AND target_key = ?
                AND claim_token = ?
                AND status = ?
            """,
            (
                NOTIFICATION_DELIVERY_SENT if success else NOTIFICATION_DELIVERY_FAILED,
                now,
                success,
                now,
                _json_text(result_payload),
                now,
                int(event_id),
                str(target_key),
                str(claim_token),
                NOTIFICATION_DELIVERY_SENDING,
            ),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            return False
        _refresh_alert_notification_status(connection, event_id=int(event_id))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def refresh_alert_notification_status(
    connection: sqlite3.Connection,
    *,
    event_ids: Sequence[int],
) -> None:
    """Rebuild legacy event aggregates from target delivery rows."""

    normalized_event_ids = tuple(dict.fromkeys(int(event_id) for event_id in event_ids))
    if not normalized_event_ids:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for event_id in normalized_event_ids:
            _refresh_alert_notification_status(connection, event_id=event_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _refresh_alert_notification_status(
    connection: sqlite3.Connection,
    *,
    event_id: int,
) -> None:
    rows = connection.execute(
        """
        SELECT target_key, channel, status, attempted_at, sent_at, result_json
        FROM notification_deliveries
        WHERE event_id = ?
        ORDER BY target_key
        """,
        (event_id,),
    ).fetchall()
    now = _utc_now_text()
    if not rows:
        existing = connection.execute(
            "SELECT notification_status FROM alert_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if (
            existing is not None
            and str(existing["notification_status"]) == ALERT_NOTIFICATION_SENT
        ):
            return
        status = ALERT_NOTIFICATION_FAILED
        attempted_at = now
        sent_at = None
        result_payload: list[dict[str, object]] = [
            {"channel": "", "success": False, "detail": "no_targets"}
        ]
    else:
        statuses = {str(row["status"]) for row in rows}
        if statuses == {NOTIFICATION_DELIVERY_SENT}:
            status = ALERT_NOTIFICATION_SENT
        elif statuses & {
            NOTIFICATION_DELIVERY_PENDING,
            NOTIFICATION_DELIVERY_SENDING,
        }:
            status = ALERT_NOTIFICATION_PENDING
        else:
            status = ALERT_NOTIFICATION_FAILED
        attempted_values = [
            str(row["attempted_at"]) for row in rows if row["attempted_at"] is not None
        ]
        sent_values = [
            str(row["sent_at"]) for row in rows if row["sent_at"] is not None
        ]
        attempted_at = max(attempted_values, default=None)
        sent_at = (
            max(sent_values, default=None)
            if status == ALERT_NOTIFICATION_SENT
            else None
        )
        result_payload = []
        for row in rows:
            try:
                value = json.loads(str(row["result_json"]))
            except (TypeError, json.JSONDecodeError):
                value = {
                    "channel": str(row["channel"]),
                    "success": str(row["status"]) == NOTIFICATION_DELIVERY_SENT,
                    "detail": "",
                }
            if not isinstance(value, dict):
                value = {"channel": str(row["channel"]), "success": False}
            value = dict(value)
            value["target_key"] = str(row["target_key"])
            result_payload.append(value)
    connection.execute(
        """
        UPDATE alert_events
        SET
            notification_status = ?,
            notification_attempted_at = ?,
            notification_sent_at = ?,
            notification_result_json = ?
        WHERE id = ?
        """,
        (status, attempted_at, sent_at, _json_text(result_payload), event_id),
    )


def get_active_drawdown_cycle(
    connection: sqlite3.Connection,
    rule_id: int,
) -> sqlite3.Row | None:
    """Return the active drawdown cycle for a rule."""

    return connection.execute(
        """
        SELECT
            id,
            rule_id,
            peak_date,
            initial_peak_price,
            peak_price,
            saw_below_peak,
            last_evaluated_date,
            end_date,
            created_at,
            updated_at
        FROM drawdown_cycles
        WHERE rule_id = ? AND end_date IS NULL
        """,
        (rule_id,),
    ).fetchone()


def list_drawdown_tier_records(
    connection: sqlite3.Connection,
    cycle_id: int,
    *,
    source: str | None = None,
) -> list[sqlite3.Row]:
    """Return durable tier records for one cycle, optionally by source."""

    filters = ["cycle_id = ?"]
    values: list[object] = [cycle_id]
    if source is not None:
        if source not in {"close_confirmed", "user_marked_added"}:
            raise ValueError("Unsupported drawdown tier record source.")
        filters.append("source = ?")
        values.append(source)

    return list(
        connection.execute(
            f"""
            SELECT
                id,
                cycle_id,
                tier_key,
                drawdown,
                amount,
                source,
                data_date,
                alert_event_id,
                created_at
            FROM drawdown_tier_records
            WHERE {" AND ".join(filters)}
            ORDER BY drawdown
            """,
            values,
        ).fetchall()
    )


def list_drawdown_tier_reminder_states(
    connection: sqlite3.Connection,
    cycle_id: int,
) -> list[sqlite3.Row]:
    """Return user reminder preferences for one drawdown cycle."""

    return list(
        connection.execute(
            """
            SELECT
                cycle_id,
                tier_key,
                skipped_for_cycle,
                snoozed_market_date,
                created_at,
                updated_at
            FROM drawdown_tier_reminder_states
            WHERE cycle_id = ?
            ORDER BY tier_key
            """,
            (cycle_id,),
        ).fetchall()
    )


def get_drawdown_tier_reminder_states(
    connection: sqlite3.Connection,
    cycle_id: int,
) -> dict[str, sqlite3.Row]:
    """Return reminder preferences keyed by their canonical tier key."""

    return {
        str(row["tier_key"]): row
        for row in list_drawdown_tier_reminder_states(connection, cycle_id)
    }


def snooze_drawdown_tiers_for_date(
    connection: sqlite3.Connection,
    *,
    cycle_id: int,
    tier_keys: Sequence[str],
    market_date: str,
) -> tuple[str, ...]:
    """Suppress selected tiers for one market date, without changing facts."""

    keys = _normalize_drawdown_tier_keys(tier_keys)
    if not keys:
        return ()
    if not market_date:
        raise ValueError("A market date is required to snooze drawdown tiers.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_drawdown_cycle(connection, cycle_id)
        now = _utc_now_text()
        connection.executemany(
            """
            INSERT INTO drawdown_tier_reminder_states (
                cycle_id,
                tier_key,
                snoozed_market_date,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id, tier_key) DO UPDATE SET
                snoozed_market_date = excluded.snoozed_market_date,
                updated_at = excluded.updated_at
            """,
            [(cycle_id, key, market_date, now, now) for key in keys],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return keys


def skip_drawdown_tiers_for_cycle(
    connection: sqlite3.Connection,
    *,
    cycle_id: int,
    tier_keys: Sequence[str],
) -> tuple[str, ...]:
    """Suppress selected tiers for the remainder of their active cycle."""

    keys = _normalize_drawdown_tier_keys(tier_keys)
    if not keys:
        return ()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_drawdown_cycle(connection, cycle_id)
        now = _utc_now_text()
        connection.executemany(
            """
            INSERT INTO drawdown_tier_reminder_states (
                cycle_id,
                tier_key,
                skipped_for_cycle,
                created_at,
                updated_at
            )
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(cycle_id, tier_key) DO UPDATE SET
                skipped_for_cycle = 1,
                snoozed_market_date = NULL,
                updated_at = excluded.updated_at
            """,
            [(cycle_id, key, now, now) for key in keys],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return keys


def clear_drawdown_tier_skip(
    connection: sqlite3.Connection,
    *,
    cycle_id: int,
    tier_keys: Sequence[str],
) -> tuple[str, ...]:
    """Clear cycle skips after a user records an actual addition."""

    keys = _normalize_drawdown_tier_keys(tier_keys)
    if not keys:
        return ()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_drawdown_cycle(connection, cycle_id)
        now = _utc_now_text()
        connection.executemany(
            """
            UPDATE drawdown_tier_reminder_states
            SET skipped_for_cycle = 0, updated_at = ?
            WHERE cycle_id = ? AND tier_key = ?
            """,
            [(now, cycle_id, key) for key in keys],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return keys


def persist_drawdown_plan_evaluation(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    expected_active_cycle_id: int | None,
    expected_last_evaluated_date: str | None,
    start_new_cycle: bool,
    peak_date: str,
    peak_price: float,
    evaluation_date: str,
    saw_below_peak: bool = False,
    tiers: Sequence[Any] = (),
    tiers_to_record: Sequence[Any] | None = None,
    tier_source: str = "close_confirmed",
    alert: Any | None = None,
    alert_factory: Any | None = None,
) -> tuple[int, int | None]:
    """Atomically persist cycle state, tier facts, and an optional event."""

    if tier_source not in {"close_confirmed", "user_marked_added"}:
        raise ValueError("Unsupported drawdown tier record source.")
    if tiers_to_record is not None and tiers:
        raise ValueError("Pass tiers or tiers_to_record, not both.")
    records = tuple(tiers if tiers_to_record is None else tiers_to_record)
    if alert is not None and alert_factory is not None:
        raise ValueError("Pass alert or alert_factory, not both.")
    record_keys = tuple(str(tier.key) for tier in records)
    if len(set(record_keys)) != len(record_keys):
        raise sqlite3.IntegrityError("Duplicate drawdown tier records.")

    connection.execute("BEGIN IMMEDIATE")
    try:
        rule = connection.execute(
            "SELECT enabled FROM rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if rule is None or not bool(rule["enabled"]):
            raise sqlite3.IntegrityError("Drawdown plan is no longer enabled.")

        active = get_active_drawdown_cycle(connection, rule_id)
        active_id = None if active is None else int(active["id"])
        active_evaluation_date = (
            None if active is None else str(active["last_evaluated_date"])
        )
        if (
            active_id != expected_active_cycle_id
            or active_evaluation_date != expected_last_evaluated_date
        ):
            raise sqlite3.IntegrityError("Active drawdown cycle changed concurrently.")

        now = _utc_now_text()
        if start_new_cycle:
            if active_id is not None:
                connection.execute(
                    """
                    UPDATE drawdown_cycles
                    SET end_date = ?, updated_at = ?
                    WHERE id = ? AND end_date IS NULL
                    """,
                    (peak_date, now, active_id),
                )
            cursor = connection.execute(
                """
                INSERT INTO drawdown_cycles (
                    rule_id,
                    peak_date,
                    initial_peak_price,
                    peak_price,
                    saw_below_peak,
                    last_evaluated_date,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    peak_date,
                    peak_price,
                    peak_price,
                    int(saw_below_peak),
                    evaluation_date,
                    now,
                    now,
                ),
            )
            cycle_id = int(cursor.lastrowid)
        else:
            if active_id is None:
                raise sqlite3.IntegrityError("No active drawdown cycle to update.")
            connection.execute(
                """
                UPDATE drawdown_cycles
                SET peak_price = ?, saw_below_peak = ?,
                    last_evaluated_date = ?, updated_at = ?
                WHERE id = ? AND end_date IS NULL
                """,
                (peak_price, int(saw_below_peak), evaluation_date, now, active_id),
            )
            cycle_id = active_id

        resolved_alert = alert_factory(cycle_id) if alert_factory is not None else alert
        event_id: int | None = None
        alert_tier_keys: set[str] | None = None
        if resolved_alert is not None:
            payload = dict(resolved_alert.get("payload") or {})
            payload["cycle_id"] = cycle_id
            alert_tiers = payload.get("actionable_tiers")
            if alert_tiers is None:
                alert_tiers = payload.get("crossed_tiers")
            if isinstance(alert_tiers, (list, tuple)):
                alert_tier_keys = {
                    str(item["key"])
                    for item in alert_tiers
                    if isinstance(item, dict) and "key" in item
                }
            cursor = connection.execute(
                """
                INSERT INTO alert_events (
                    rule_id,
                    alert_key,
                    title,
                    message,
                    payload_json,
                    triggered_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    str(resolved_alert["alert_key"]),
                    str(resolved_alert["title"]),
                    str(resolved_alert["message"]),
                    _json_text(payload),
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
        if records:
            connection.executemany(
                """
                INSERT INTO drawdown_tier_records (
                    cycle_id,
                    tier_key,
                    drawdown,
                    amount,
                    source,
                    data_date,
                    alert_event_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, tier_key) DO UPDATE SET
                    drawdown = excluded.drawdown,
                    amount = excluded.amount,
                    source = CASE
                        WHEN excluded.source = 'close_confirmed'
                        THEN excluded.source
                        ELSE drawdown_tier_records.source
                    END,
                    data_date = CASE
                        WHEN excluded.source = 'close_confirmed'
                        THEN excluded.data_date
                        ELSE drawdown_tier_records.data_date
                    END,
                    alert_event_id = CASE
                        WHEN excluded.source = 'close_confirmed'
                        THEN excluded.alert_event_id
                        ELSE drawdown_tier_records.alert_event_id
                    END
                """,
                [
                    (
                        cycle_id,
                        str(tier.key),
                        float(tier.drawdown),
                        float(tier.amount),
                        tier_source,
                        evaluation_date,
                        (
                            event_id
                            if alert_tier_keys is None
                            or str(tier.key) in alert_tier_keys
                            else None
                        ),
                        now,
                    )
                    for tier in records
                ],
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return cycle_id, event_id


def list_retryable_drawdown_plan_alert_events(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return pending or failed plan reminders that still need delivery."""

    return list(
        connection.execute(
            """
            SELECT
                e.id,
                e.title,
                e.message,
                e.notification_status,
                json_extract(e.payload_json, '$.data_date') AS data_date
            FROM alert_events AS e
            JOIN rules AS r ON r.id = e.rule_id
            WHERE
                e.notification_status IN (?, ?)
                AND (
                    (
                        r.type = 'drawdown_plan'
                        AND COALESCE(
                            json_extract(e.payload_json, '$.phase'), ''
                        ) != 'before_close'
                    )
                    OR json_extract(e.payload_json, '$.phase') = 'fund_nav'
                )
            ORDER BY e.id
            """,
            (ALERT_NOTIFICATION_PENDING, ALERT_NOTIFICATION_FAILED),
        ).fetchall()
    )


def list_retryable_position_profit_alert_events(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return undelivered position-linked Price-Gain reminders."""

    return list(
        connection.execute(
            """
            SELECT e.id, e.title, e.message
            FROM alert_events AS e
            JOIN rules AS r ON r.id = e.rule_id
            WHERE
                r.type = 'profit_reminder'
                AND json_extract(e.payload_json, '$.phase') = 'position_profit'
                AND e.notification_status IN (?, ?)
            ORDER BY e.id
            """,
            (ALERT_NOTIFICATION_PENDING, ALERT_NOTIFICATION_FAILED),
        ).fetchall()
    )


def list_retryable_standard_alert_events(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return undelivered standard drawdown, price-gain, and DCA reminders."""

    return list(
        connection.execute(
            """
            SELECT
                e.id,
                e.title,
                e.message,
                e.rule_id,
                CASE e.title
                    WHEN 'DCA reminder' THEN 'dca_reminder'
                    WHEN 'Drawdown reminder' THEN 'drawdown_from_high'
                    WHEN 'Price-Gain reminder' THEN 'profit_reminder'
                END AS rule_type,
                json_extract(e.payload_json, '$.due_date') AS due_date,
                json_extract(e.payload_json, '$.amount') AS dca_amount,
                json_extract(e.payload_json, '$.fund_symbol') AS fund_symbol,
                o.status AS occurrence_status,
                o.effective_date AS occurrence_effective_date
            FROM alert_events AS e
            LEFT JOIN scheduled_dca_occurrences AS o
                ON o.rule_id = e.rule_id
                AND o.due_date = json_extract(e.payload_json, '$.due_date')
                AND o.fund_symbol = json_extract(
                    e.payload_json, '$.fund_symbol'
                )
            WHERE
                e.notification_status IN (?, ?)
                AND (
                    (
                        e.title IN (
                            'DCA reminder',
                            'Drawdown reminder',
                            'Price-Gain reminder'
                        )
                        AND COALESCE(
                            json_extract(e.payload_json, '$.phase'), ''
                        ) = ''
                    )
                    OR (
                        e.title = 'Reminder recovery notice'
                        AND json_extract(e.payload_json, '$.phase') =
                            'standard_recovery'
                    )
                )
            ORDER BY e.id
            """,
            (ALERT_NOTIFICATION_PENDING, ALERT_NOTIFICATION_FAILED),
        ).fetchall()
    )


def get_position_profit_event(
    connection: sqlite3.Connection,
    event_id: int,
) -> sqlite3.Row | None:
    """Return one position-linked Price-Gain event and current position state."""

    return connection.execute(
        """
        SELECT
            e.id,
            e.payload_json,
            r.enabled,
            r.symbol,
            c.id AS active_cycle_id,
            p.units,
            p.average_unit_cost
        FROM alert_events AS e
        JOIN rules AS r ON r.id = e.rule_id
        LEFT JOIN position_cycles AS c
            ON c.fund_symbol = r.symbol AND c.ended_at IS NULL
        LEFT JOIN position_snapshots AS p ON p.fund_symbol = r.symbol
        WHERE
            e.id = ?
            AND r.type = 'profit_reminder'
            AND json_extract(e.payload_json, '$.phase') = 'position_profit'
        """,
        (event_id,),
    ).fetchone()


def close_position_from_profit_event(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    synced_at: str | datetime | None = None,
) -> bool:
    """Close the still-active position cycle referenced by a reminder."""

    sync_time = _timestamp_text(synced_at)
    now = _utc_now_text()
    connection.execute("BEGIN IMMEDIATE")
    try:
        event = get_position_profit_event(connection, event_id)
        if event is None:
            raise sqlite3.IntegrityError("Price-Gain reminder was not found.")
        payload = json.loads(str(event["payload_json"]))
        expected_cycle_id = int(payload["position_cycle_id"])
        if event["active_cycle_id"] is None:
            connection.rollback()
            return False
        if (
            int(event["active_cycle_id"]) != expected_cycle_id
            or float(event["units"] or 0) <= 0
        ):
            raise sqlite3.IntegrityError(
                "Position changed since this reminder; rerun /sync_position."
            )
        pending_items = list_pending_position_items(connection, str(event["symbol"]))
        if pending_items:
            raise sqlite3.IntegrityError(
                f"Pending additions exist; run /sync_position {event['symbol']} 0 0 "
                "to classify them before closing."
            )
        connection.execute(
            """
            UPDATE position_snapshots
            SET
                units = 0,
                average_unit_cost = 0,
                is_estimated = 0,
                last_synced_at = ?,
                estimates_since_sync = 0,
                position_sync_required_since = NULL,
                updated_at = ?
            WHERE fund_symbol = ?
            """,
            (sync_time, now, str(event["symbol"])),
        )
        _maintain_position_cycle(
            connection,
            fund_symbol=str(event["symbol"]),
            new_units=0,
            changed_at=sync_time,
        )
        connection.execute(
            """
            UPDATE fund_settings
            SET position_sync_required_since = NULL, updated_at = ?
            WHERE fund_symbol = ?
            """,
            (now, str(event["symbol"])),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def _ensure_alert_event_delivery_columns(connection: sqlite3.Connection) -> bool:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(alert_events)").fetchall()
    }
    column_definitions = {
        "notification_status": "TEXT NOT NULL DEFAULT 'pending'",
        "notification_attempted_at": "TEXT",
        "notification_sent_at": "TEXT",
        "notification_result_json": "TEXT",
    }
    delivery_columns_added = "notification_status" not in columns
    for column, definition in column_definitions.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE alert_events ADD COLUMN {column} {definition}"
            )
    return delivery_columns_added


_MONOTONIC_ID_TABLES = ("rules", "alert_events")


def _migrate_monotonic_ids(connection: sqlite3.Connection) -> None:
    """Upgrade legacy rowids without ever reusing rule or event IDs.

    SQLite cannot add AUTOINCREMENT to an existing table in place.  The table
    is therefore rebuilt from its original schema while foreign-key checks are
    disabled outside the migration transaction.  Child tables keep referring
    to the original table names because those names are never renamed away.
    """

    schemas = {
        table: _table_schema_sql(connection, table) for table in _MONOTONIC_ID_TABLES
    }
    targets = [
        (table, schema)
        for table, schema in schemas.items()
        if schema is not None and not _schema_has_autoincrement(schema)
    ]
    if not targets:
        return

    # PRAGMA foreign_keys is a connection setting and is ignored mid-
    # transaction.  Finish any setup DML before changing it.
    connection.commit()
    original_foreign_keys = bool(
        connection.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    migration_committed = False
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("Could not disable SQLite foreign-key checks.")

        connection.execute("BEGIN IMMEDIATE")
        try:
            for table, schema in targets:
                _rebuild_table_with_autoincrement(
                    connection,
                    table=table,
                    schema_sql=schema,
                )
                _seed_autoincrement_high_water(connection, table)
            _assert_foreign_keys_clean(connection)
            connection.commit()
            migration_committed = True
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

        # Successful upgrades leave FK enforcement enabled, even if a caller
        # happened to open this legacy connection with it disabled.
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Could not enable SQLite foreign-key checks.")
        _assert_foreign_keys_clean(connection)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if connection.in_transaction:
            connection.rollback()
        desired_foreign_keys = (
            "ON" if migration_committed else ("ON" if original_foreign_keys else "OFF")
        )
        connection.execute(f"PRAGMA foreign_keys = {desired_foreign_keys}")


def _table_schema_sql(
    connection: sqlite3.Connection,
    table: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _schema_has_autoincrement(schema_sql: str) -> bool:
    return re.search(r"\bAUTOINCREMENT\b", schema_sql, re.IGNORECASE) is not None


def _rebuild_table_with_autoincrement(
    connection: sqlite3.Connection,
    *,
    table: str,
    schema_sql: str,
) -> None:
    """Copy one table using its own schema, changing only its integer key."""

    temporary_table = f"{table}__autoincrement_migration"
    table_columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    ]
    if not table_columns:
        raise RuntimeError(f"Cannot migrate missing or empty schema for {table!r}.")

    temporary_schema = _migration_schema_sql(
        schema_sql,
        table=table,
        temporary_table=temporary_table,
    )
    connection.execute(temporary_schema)
    columns = ", ".join(_quote_identifier(column) for column in table_columns)
    connection.execute(
        f"INSERT INTO {_quote_identifier(temporary_table)} ({columns}) "
        f"SELECT {columns} FROM {_quote_identifier(table)}"
    )
    connection.execute(f"DROP TABLE {_quote_identifier(table)}")
    connection.execute(
        f"ALTER TABLE {_quote_identifier(temporary_table)} "
        f"RENAME TO {_quote_identifier(table)}"
    )


def _migration_schema_sql(
    schema_sql: str,
    *,
    table: str,
    temporary_table: str,
) -> str:
    table_pattern = re.compile(
        rf"(\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)("
        rf'"{re.escape(table)}"|`{re.escape(table)}`|'
        rf"\[{re.escape(table)}\]|{re.escape(table)})(\b)",
        re.IGNORECASE,
    )
    table_match = table_pattern.search(schema_sql)
    if table_match is None:
        raise RuntimeError(f"Unsupported SQLite schema for {table!r}.")

    name_token = table_match.group(2)
    if name_token.startswith('"'):
        temporary_token = f'"{temporary_table}"'
    elif name_token.startswith("`"):
        temporary_token = f"`{temporary_table}`"
    elif name_token.startswith("["):
        temporary_token = f"[{temporary_table}]"
    else:
        temporary_token = temporary_table
    migrated = (
        schema_sql[: table_match.start(2)]
        + temporary_token
        + schema_sql[table_match.end(2) :]
    )
    id_pattern = re.compile(
        r"(\bid\s+INTEGER\s+PRIMARY\s+KEY)(?!\s+AUTOINCREMENT)",
        re.IGNORECASE,
    )
    migrated, replacements = id_pattern.subn(r"\1 AUTOINCREMENT", migrated, count=1)
    if replacements != 1:
        raise RuntimeError(f"Unsupported integer primary key schema for {table!r}.")
    return migrated


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _assert_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            f"SQLite foreign-key check failed after schema migration: {violations!r}"
        )


def _seed_autoincrement_high_water(
    connection: sqlite3.Connection,
    table: str,
) -> None:
    """Keep IDs above references to rows removed before this migration."""

    references = {
        "rules": (
            ("alert_events", "rule_id"),
            ("drawdown_cycles", "rule_id"),
            ("position_profit_thresholds", "rule_id"),
            ("position_profit_evaluations", "rule_id"),
            ("manual_add_estimates", "rule_id"),
            ("scheduled_dca_occurrences", "rule_id"),
        ),
        "alert_events": (
            ("notification_deliveries", "event_id"),
            ("drawdown_tier_records", "alert_event_id"),
            ("position_profit_thresholds", "alert_event_id"),
            ("manual_add_estimates", "source_alert_event_id"),
            ("manual_add_estimates", "settlement_alert_event_id"),
            ("manual_add_actions", "source_alert_event_id"),
        ),
    }[table]
    high_water = _max_column_value(connection, table, "id")
    for reference_table, reference_column in references:
        high_water = max(
            high_water,
            _max_column_value(connection, reference_table, reference_column),
        )
    if high_water <= 0:
        return
    cursor = connection.execute(
        """
        UPDATE sqlite_sequence
        SET seq = ?
        WHERE name = ? AND COALESCE(seq, 0) < ?
        """,
        (high_water, table, high_water),
    )
    if (
        cursor.rowcount == 0
        and connection.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name = ?",
            (table,),
        ).fetchone()
        is None
    ):
        connection.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            (table, high_water),
        )


def _max_column_value(
    connection: sqlite3.Connection,
    table: str,
    column: str,
) -> int:
    row = connection.execute(
        f"SELECT MAX({_quote_identifier(column)}) FROM {_quote_identifier(table)}"
    ).fetchone()
    value = row[0]
    return 0 if value is None else int(value)


def _ensure_drawdown_cycle_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(drawdown_cycles)").fetchall()
    }
    if "saw_below_peak" not in columns:
        connection.execute(
            "ALTER TABLE drawdown_cycles ADD COLUMN saw_below_peak INTEGER "
            "NOT NULL DEFAULT 0 CHECK (saw_below_peak IN (0, 1))"
        )


def _ensure_standard_notification_recovery_migration(
    connection: sqlite3.Connection,
    *,
    delivery_columns_added: bool,
    now: str,
) -> None:
    if connection.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (STANDARD_NOTIFICATION_RECOVERY_MIGRATION_KEY,),
    ).fetchone():
        return

    first_tracked_id = None
    if not delivery_columns_added:
        first_tracked_id = connection.execute(
            """
            SELECT MIN(id) FROM alert_events
            WHERE notification_attempted_at IS NOT NULL
                AND COALESCE(json_extract(payload_json, '$.phase'), '') = ''
                AND title IN (
                    'DCA reminder',
                    'Drawdown reminder',
                    'Price-Gain reminder'
                )
            """
        ).fetchone()[0]
    # Pre-v1 pending rows have no per-event provenance; never guess that an
    # investment reminder is current when this database has no attempt boundary.
    id_filter = "" if first_tracked_id is None else "AND id < ?"
    params: tuple[object, ...] = (
        ALERT_NOTIFICATION_PENDING,
        *(() if first_tracked_id is None else (int(first_tracked_id),)),
    )
    ambiguous = connection.execute(
        f"""
        SELECT COUNT(*) AS event_count, MIN(rule_id) AS rule_id
        FROM alert_events
        WHERE notification_status = ?
            AND notification_attempted_at IS NULL
            AND COALESCE(json_extract(payload_json, '$.phase'), '') = ''
            AND title IN (
                'DCA reminder',
                'Drawdown reminder',
                'Price-Gain reminder'
            )
            {id_filter}
        """,
        params,
    ).fetchone()
    ambiguous_count = int(ambiguous["event_count"])
    if ambiguous_count:
        connection.execute(
            f"""
            UPDATE alert_events
            SET notification_status = ?
            WHERE notification_status = ?
                AND notification_attempted_at IS NULL
                AND COALESCE(json_extract(payload_json, '$.phase'), '') = ''
                AND title IN (
                    'DCA reminder',
                    'Drawdown reminder',
                    'Price-Gain reminder'
                )
                {id_filter}
            """,
            (ALERT_NOTIFICATION_SENT, *params),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO alert_events (
                rule_id, alert_key, title, message, payload_json, triggered_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(ambiguous["rule_id"]),
                "standard_notification_recovery:v1",
                STANDARD_NOTIFICATION_RECOVERY_NOTICE_TITLE,
                (
                    "⚠️ Reminder recovery notice\n\n"
                    f"{ambiguous_count} older reminder(s) had no reliable "
                    "delivery record during database upgrade. They were not "
                    "replayed to avoid stale duplicate reminders.\n\n"
                    "Please run /check to review the current state."
                ),
                _json_text(
                    {
                        "phase": "standard_recovery",
                        "ambiguous_event_count": ambiguous_count,
                    }
                ),
                now,
            ),
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO app_metadata (key, value, updated_at)
        VALUES (?, 'complete', ?)
        """,
        (STANDARD_NOTIFICATION_RECOVERY_MIGRATION_KEY, now),
    )


def _ensure_fund_settings_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(fund_settings)").fetchall()
    }
    if "position_sync_required_since" not in columns:
        connection.execute(
            "ALTER TABLE fund_settings ADD COLUMN position_sync_required_since TEXT"
        )


def _notification_result_payload(result: Any) -> dict[str, object]:
    return {
        "channel": str(_read_result_value(result, "channel", "")),
        "success": bool(_read_result_value(result, "success", False)),
        "detail": str(_read_result_value(result, "detail", "")),
    }


def _read_result_value(result: Any, key: str, default: Any) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _normalize_drawdown_tier_keys(tier_keys: Sequence[str]) -> tuple[str, ...]:
    """Validate and de-duplicate tier keys while preserving caller order."""

    keys: list[str] = []
    for raw_key in tier_keys:
        key = str(raw_key).strip()
        if not key:
            raise ValueError("Drawdown tier keys must not be empty.")
        try:
            number = float(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("Drawdown tier keys must be finite numbers.") from exc
        if not math.isfinite(number):
            raise ValueError("Drawdown tier keys must be finite numbers.")
        keys.append(format(Decimal(str(number)).normalize(), "f"))
    keys = tuple(keys)
    if len(set(keys)) != len(keys):
        raise ValueError("Drawdown tier keys must be unique.")
    return keys


def _require_drawdown_cycle(
    connection: sqlite3.Connection,
    cycle_id: int,
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM drawdown_cycles WHERE id = ?",
            (cycle_id,),
        ).fetchone()
        is None
    ):
        raise sqlite3.IntegrityError("Drawdown cycle was not found.")


def _timestamp_text(value: str | datetime | None) -> str:
    if value is None:
        return _utc_now_text()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(microsecond=0).isoformat()
    return value


def _utc_now_text() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
