import json
import sqlite3
from pathlib import Path

import pytest

from fund_alert_bot.db import (
    add_alert_event,
    add_rule,
    alert_exists,
    delete_rule,
    init_db,
    list_enabled_rules,
    list_rules,
    open_connection,
)


def test_init_db_creates_storage_tables(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name
            """
        ).fetchall()

    table_names = {row["name"] for row in rows}
    assert {
        "alert_events",
        "app_metadata",
        "notification_channels",
        "rules",
    }.issubset(table_names)


def test_init_db_creates_required_rule_columns(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rows = connection.execute("PRAGMA table_info(rules)").fetchall()

    columns = {row["name"]: row for row in rows}
    assert set(columns) == {
        "id",
        "type",
        "symbol",
        "name",
        "asset_type",
        "params_json",
        "enabled",
        "created_at",
        "updated_at",
    }
    assert columns["enabled"]["dflt_value"] == "1"


def test_init_db_creates_required_event_and_channel_columns(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        event_rows = connection.execute("PRAGMA table_info(alert_events)").fetchall()
        channel_rows = connection.execute(
            "PRAGMA table_info(notification_channels)"
        ).fetchall()

    event_columns = {row["name"]: row for row in event_rows}
    channel_columns = {row["name"]: row for row in channel_rows}
    assert set(event_columns) == {
        "id",
        "rule_id",
        "alert_key",
        "title",
        "message",
        "payload_json",
        "triggered_at",
    }
    assert set(channel_columns) == {
        "id",
        "type",
        "name",
        "config_json",
        "enabled",
        "created_at",
        "updated_at",
    }
    assert channel_columns["enabled"]["dflt_value"] == "1"


def test_rule_helpers_add_list_filter_and_delete_rules(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        enabled_rule_id = add_rule(
            connection,
            type="drawdown",
            symbol="510300",
            name="CSI 300 ETF drawdown",
            asset_type="fund",
            params={"drawdown_pct": 10},
        )
        disabled_rule_id = add_rule(
            connection,
            type="dca",
            symbol="159915",
            name="ChiNext ETF DCA",
            asset_type="fund",
            params={"weekday": "Friday"},
            enabled=False,
        )

        rows = list_rules(connection)
        enabled_rows = list_enabled_rules(connection)
        deleted = delete_rule(connection, disabled_rule_id)
        deleted_again = delete_rule(connection, disabled_rule_id)

    assert [row["id"] for row in rows] == [enabled_rule_id, disabled_rule_id]
    assert json.loads(rows[0]["params_json"]) == {"drawdown_pct": 10}
    assert rows[0]["enabled"] == 1
    assert rows[0]["created_at"] == rows[0]["updated_at"]
    assert [row["id"] for row in enabled_rows] == [enabled_rule_id]
    assert deleted
    assert not deleted_again


def test_alert_event_helpers_store_payload_and_detect_existing_alerts(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type="profit_taking",
            symbol="510500",
            name="CSI 500 ETF profit reminder",
            asset_type="fund",
            params={"gain_pct": 20},
        )

        assert not alert_exists(connection, "profit_taking:510500:2026-06-05")

        event_id = add_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="profit_taking:510500:2026-06-05",
            title="Profit-taking reminder",
            message="510500 reached the configured reminder threshold.",
            payload={"gain_pct": 21.5},
            triggered_at="2026-06-05T10:00:00+00:00",
        )
        row = connection.execute(
            """
            SELECT *
            FROM alert_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()

        assert alert_exists(connection, "profit_taking:510500:2026-06-05")

    assert row["rule_id"] == rule_id
    assert row["alert_key"] == "profit_taking:510500:2026-06-05"
    assert json.loads(row["payload_json"]) == {"gain_pct": 21.5}
    assert row["triggered_at"] == "2026-06-05T10:00:00+00:00"


def test_alert_key_is_unique(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type="drawdown",
            symbol="510300",
            name="CSI 300 ETF drawdown",
            asset_type="fund",
            params={"drawdown_pct": 10},
        )
        add_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="drawdown:510300:2026-06-05",
            title="Drawdown reminder",
            message="510300 crossed the configured drawdown threshold.",
        )

        with pytest.raises(sqlite3.IntegrityError):
            add_alert_event(
                connection,
                rule_id=rule_id,
                alert_key="drawdown:510300:2026-06-05",
                title="Drawdown reminder",
                message="Duplicate alert key.",
            )
