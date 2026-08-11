import json
import sqlite3
from pathlib import Path

import pytest

from fund_alert_bot.db import (
    ALERT_NOTIFICATION_FAILED,
    ALERT_NOTIFICATION_PENDING,
    ALERT_NOTIFICATION_SENT,
    add_alert_event,
    add_drawdown_plan_rule,
    add_rule,
    alert_exists,
    connect,
    delete_rule,
    get_fund_settings,
    get_position_snapshot,
    init_db,
    list_enabled_rules,
    list_retryable_standard_alert_events,
    list_rules,
    open_connection,
    record_alert_notification_result,
    reserve_alert_event,
    upsert_fund_cutoff,
    upsert_fund_fee,
    upsert_position_snapshot,
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
        "fund_settings",
        "notification_channels",
        "position_snapshots",
        "rules",
    }.issubset(table_names)


def test_fund_settings_and_position_snapshot_survive_restart(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        upsert_fund_fee(
            connection,
            fund_symbol="110026",
            fee_mode="rate",
            fee_value=0.0015,
        )
        upsert_fund_cutoff(
            connection,
            fund_symbol="110026",
            subscription_cutoff="14:45",
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="110026",
            units=1234.5,
            average_unit_cost=1.234,
            synced_at="2026-08-09T01:02:03+00:00",
        )

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        settings = get_fund_settings(connection, "110026")
        position = get_position_snapshot(connection, "110026")

    assert dict(settings) == {
        "fund_symbol": "110026",
        "fee_mode": "rate",
        "fee_value": 0.0015,
        "subscription_cutoff": "14:45",
        "position_sync_required_since": None,
        "created_at": settings["created_at"],
        "updated_at": settings["updated_at"],
    }
    assert position["units"] == 1234.5
    assert position["average_unit_cost"] == 1.234
    assert position["is_estimated"] == 0
    assert position["last_synced_at"] == "2026-08-09T01:02:03+00:00"
    assert position["estimates_since_sync"] == 0


def test_init_adds_position_tables_without_changing_existing_rules(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type="dca_reminder",
            symbol="A500",
            name="A500",
            asset_type="dca",
            params={"weekday": "FRI", "amount": 2000},
        )
        connection.execute("DROP TABLE fund_settings")
        connection.execute("DROP TABLE position_snapshots")
        connection.commit()

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        rules = list_rules(connection)

    assert {"fund_settings", "position_snapshots"}.issubset(tables)
    assert [row["id"] for row in rules] == [rule_id]


def test_position_sync_accepts_exact_closed_pair_and_resets_estimate_state() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO position_snapshots (
                fund_symbol, units, average_unit_cost, is_estimated,
                last_synced_at, estimates_since_sync,
                position_sync_required_since, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, 3, ?, ?, ?)
            """,
            (
                "110026",
                100,
                1.2,
                "2026-08-01T00:00:00+00:00",
                "2026-08-02T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            ),
        )
        connection.commit()

        row = upsert_position_snapshot(
            connection,
            fund_symbol="110026",
            units=0,
            average_unit_cost=0,
            synced_at="2026-08-09T00:00:00+00:00",
        )
    finally:
        connection.close()

    assert row["units"] == 0
    assert row["average_unit_cost"] == 0
    assert row["is_estimated"] == 0
    assert row["estimates_since_sync"] == 0
    assert row["position_sync_required_since"] is None


def test_position_table_rejects_mixed_zero_values() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        with pytest.raises(sqlite3.IntegrityError):
            upsert_position_snapshot(
                connection,
                fund_symbol="110026",
                units=10,
                average_unit_cost=0,
            )
    finally:
        connection.close()


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
        "notification_status",
        "notification_attempted_at",
        "notification_sent_at",
        "notification_result_json",
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


def test_init_db_does_not_recover_ambiguous_preexisting_history(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        connection.execute(
            """
            CREATE TABLE alert_events (
                id INTEGER PRIMARY KEY,
                rule_id INTEGER NOT NULL,
                alert_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT,
                triggered_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO alert_events (
                rule_id, alert_key, title, message, triggered_at
            ) VALUES (99, 'old-drawdown', 'Drawdown reminder', 'already sent',
                      '2024-01-01T00:00:00+00:00')
            """
        )
        init_db(connection)

        assert list_retryable_standard_alert_events(connection) == []
        connection.execute(
            "UPDATE alert_events SET notification_status = 'failed' WHERE id = 1"
        )
        assert [
            int(row["id"]) for row in list_retryable_standard_alert_events(connection)
        ] == [1]
        connection.execute(
            "UPDATE alert_events SET notification_status = 'sent' WHERE id = 1"
        )

        rule_id = add_rule(
            connection,
            type="drawdown_from_high",
            symbol="399006",
            name="ChiNext",
            asset_type="cn_index",
            params={"lookback_days": 365, "thresholds": [0.15]},
        )
        new_event_id = reserve_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="new-drawdown",
            title="Drawdown reminder",
            message="not sent yet",
        )

        assert [
            int(row["id"]) for row in list_retryable_standard_alert_events(connection)
        ] == [new_event_id]


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


def test_drawdown_plan_pair_is_unique_while_enabled() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        first_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="A500",
            params={"investment_fund_symbol": "000001", "tiers": []},
        )
        with pytest.raises(sqlite3.IntegrityError, match="already uses"):
            add_drawdown_plan_rule(
                connection,
                reference_symbol="510300",
                investment_fund_symbol="000002",
                name="same ETF",
                params={"investment_fund_symbol": "000002", "tiers": []},
            )
        with pytest.raises(sqlite3.IntegrityError, match="already uses"):
            add_drawdown_plan_rule(
                connection,
                reference_symbol="510500",
                investment_fund_symbol="000001",
                name="same fund",
                params={"investment_fund_symbol": "000001", "tiers": []},
            )

        assert delete_rule(connection, first_id)
        replacement_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="replacement",
            params={"investment_fund_symbol": "000001", "tiers": []},
        )
    finally:
        connection.close()

    assert replacement_id != first_id


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
    assert row["notification_status"] == ALERT_NOTIFICATION_PENDING


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


def test_failed_alert_delivery_is_retryable(tmp_path: Path) -> None:
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
        event_id = reserve_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="drawdown:510300:retry",
            title="Drawdown reminder",
            message="510300 crossed the configured drawdown threshold.",
        )

        assert alert_exists(connection, "drawdown:510300:retry")

        record_alert_notification_result(
            connection,
            event_id=event_id,
            results=[{"channel": "telegram", "success": False, "detail": "failed"}],
        )
        failed_row = connection.execute(
            "SELECT notification_status FROM alert_events WHERE id = ?",
            (event_id,),
        ).fetchone()

        assert not alert_exists(connection, "drawdown:510300:retry")

        retried_event_id = reserve_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="drawdown:510300:retry",
            title="Drawdown reminder",
            message="Retrying the alert.",
        )

        record_alert_notification_result(
            connection,
            event_id=retried_event_id,
            results=[{"channel": "telegram", "success": True, "detail": "sent"}],
        )
        sent_row = connection.execute(
            """
            SELECT notification_status, notification_result_json
            FROM alert_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()

        assert alert_exists(connection, "drawdown:510300:retry")

    assert failed_row["notification_status"] == ALERT_NOTIFICATION_FAILED
    assert retried_event_id == event_id
    assert sent_row["notification_status"] == ALERT_NOTIFICATION_SENT
    assert json.loads(sent_row["notification_result_json"]) == [
        {"channel": "telegram", "detail": "sent", "success": True}
    ]
