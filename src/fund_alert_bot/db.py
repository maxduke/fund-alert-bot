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
