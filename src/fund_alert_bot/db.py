"""SQLite storage helpers."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fund_alert_bot.rules.drawdown_plan import format_plan_percent

ALERT_NOTIFICATION_PENDING = "pending"
ALERT_NOTIFICATION_SENT = "sent"
ALERT_NOTIFICATION_FAILED = "failed"
RETRYABLE_ALERT_NOTIFICATION_STATUSES = frozenset({ALERT_NOTIFICATION_FAILED})
SUPPRESSING_ALERT_NOTIFICATION_STATUSES = (
    ALERT_NOTIFICATION_PENDING,
    ALERT_NOTIFICATION_SENT,
)


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
            id INTEGER PRIMARY KEY,
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
            id INTEGER PRIMARY KEY,
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
        """
    )
    _ensure_alert_event_delivery_columns(connection)
    _ensure_fund_settings_columns(connection)
    connection.commit()


def initialize_database(connection: sqlite3.Connection) -> None:
    """Backward-compatible alias for database initialization."""
    init_db(connection)


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


def delete_rule(connection: sqlite3.Connection, rule_id: int) -> bool:
    """Delete a legacy rule or disable a stateful drawdown plan."""

    row = connection.execute(
        "SELECT type FROM rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    if row is None:
        return False

    if row["type"] == "drawdown_plan":
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
        (
            fund_symbol,
            units,
            average_unit_cost,
            sync_time,
            now,
            now,
        ),
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
            ORDER BY fund_symbol
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
            EXISTS (
                SELECT 1
                FROM json_each(e.payload_json, '$.crossed_tiers') AS tier
                WHERE json_extract(tier.value, '$.key') = ?
            )
            """
        )
        values.append(tier_key)
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
            AND json_type(e.payload_json, '$.crossed_tiers') = 'array'
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
    cutoff_choice: str | None = None,
    effective_date: str | None = None,
) -> tuple[int | None, tuple[str, ...]]:
    """Record selected tiers and optionally one fee-aware pending estimate."""

    if not tiers:
        raise ValueError("At least one drawdown tier must be selected.")
    if create_estimate and (
        cutoff_choice not in {"before", "after"} or effective_date is None
    ):
        raise ValueError("A pending estimate requires a cutoff choice and date.")

    action_text = _timestamp_text(action_at)
    action_date = action_at.date().isoformat()
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
        eligible_tiers = {
            str(item["key"]): item for item in payload.get("crossed_tiers", ())
        }
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
                    str(settings["subscription_cutoff"]),
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
    included: bool,
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
        if included:
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
        message = "\n".join(
            (
                "✅ Manual addition estimate updated",
                "",
                f"Fund: {occurrence['fund_symbol']}",
                "Configured tiers: "
                + ", ".join("-" + format_plan_percent(str(key)) for key in tier_keys),
                f"Gross amount: ¥{gross_amount:,.2f}",
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
                notification_attempted_at = NULL,
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
) -> list[sqlite3.Row]:
    """Return durable tier records for one cycle."""

    return list(
        connection.execute(
            """
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
            WHERE cycle_id = ?
            ORDER BY drawdown
            """,
            (cycle_id,),
        ).fetchall()
    )


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
    tiers: Sequence[Any] = (),
    tier_source: str = "close_confirmed",
    alert: Any | None = None,
) -> tuple[int, int | None]:
    """Atomically persist cycle state, tier records, and one aggregate event."""

    if tier_source not in {"close_confirmed", "user_marked_added"}:
        raise ValueError("Unsupported drawdown tier record source.")
    if bool(tiers) != (alert is not None):
        raise ValueError(
            "An aggregate alert is required exactly when tiers are stored."
        )

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
                    last_evaluated_date,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    peak_date,
                    peak_price,
                    peak_price,
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
                SET peak_price = ?, last_evaluated_date = ?, updated_at = ?
                WHERE id = ? AND end_date IS NULL
                """,
                (peak_price, evaluation_date, now, active_id),
            )
            cycle_id = active_id

        event_id: int | None = None
        if alert is not None:
            payload = dict(alert.get("payload") or {})
            payload["cycle_id"] = cycle_id
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
                    _json_text(payload),
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
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
                """,
                [
                    (
                        cycle_id,
                        str(tier.key),
                        float(tier.drawdown),
                        float(tier.amount),
                        tier_source,
                        evaluation_date,
                        event_id,
                        now,
                    )
                    for tier in tiers
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
            SELECT e.id, e.title, e.message, e.notification_status
            FROM alert_events AS e
            JOIN rules AS r ON r.id = e.rule_id
            WHERE
                r.type = 'drawdown_plan'
                AND e.notification_status IN (?, ?)
                AND COALESCE(json_extract(e.payload_json, '$.phase'), '')
                    != 'before_close'
            ORDER BY e.id
            """,
            (ALERT_NOTIFICATION_PENDING, ALERT_NOTIFICATION_FAILED),
        ).fetchall()
    )


def _ensure_alert_event_delivery_columns(connection: sqlite3.Connection) -> None:
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
    for column, definition in column_definitions.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE alert_events ADD COLUMN {column} {definition}"
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
