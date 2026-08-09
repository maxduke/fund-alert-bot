"""SQLite storage helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        """
    )
    _ensure_alert_event_delivery_columns(connection)
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
