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
    """Delete a rule by ID and report whether a row was removed."""
    cursor = connection.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    connection.commit()
    return cursor.rowcount > 0


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
        active = get_active_drawdown_cycle(connection, rule_id)
        active_id = None if active is None else int(active["id"])
        if active_id != expected_active_cycle_id:
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
