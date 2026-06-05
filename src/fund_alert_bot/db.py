"""SQLite storage helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def connect(sqlite_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection and enable basic safety defaults."""
    path = Path(sqlite_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
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
            triggered_at TEXT NOT NULL
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
    """Return whether an alert event with the given unique key exists."""
    row = connection.execute(
        """
        SELECT 1
        FROM alert_events
        WHERE alert_key = ?
        LIMIT 1
        """,
        (alert_key,),
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
