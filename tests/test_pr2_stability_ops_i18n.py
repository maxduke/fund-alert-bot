"""Regression tests for PR 2 stability, async, i18n, and ops fixes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from fund_alert_bot import checks, commands, i18n, plan_views, scheduler
from fund_alert_bot.db import (
    ALERT_NOTIFICATION_CANCELLED,
    ALERT_NOTIFICATION_FAILED,
    CURRENT_SCHEMA_VERSION,
    add_alert_event,
    add_drawdown_plan_rule,
    add_rule,
    delete_rule,
    ensure_notification_delivery_targets,
    get_fund_settings,
    get_position_snapshot,
    initialize_database,
    list_pending_position_items,
    open_connection,
    prune_database,
    reconcile_position_snapshot,
    record_manual_addition,
)
from fund_alert_bot.market_data.index_valuation import IndexValuation
from fund_alert_bot.notifications.base import NotificationMessage
from fund_alert_bot.notifications.http_delivery import post_notification
from fund_alert_bot.notifications.ntfy import NtfyNotificationChannel
from fund_alert_bot.rules.drawdown_plan import DrawdownTier
from fund_alert_bot.valuation import get_valuation_context


def test_init_db_skips_write_locks_when_schema_version_is_current(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )

    # Open a writer lock on connection 1; a second connection calling init_db
    # must NOT block or fail with OperationalError because user_version matches.
    writer = sqlite3.connect(sqlite_path, timeout=0.1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        reader = sqlite3.connect(sqlite_path, timeout=0.1)
        try:
            reader.row_factory = sqlite3.Row
            initialize_database(reader)
        finally:
            reader.close()
        writer.rollback()
    finally:
        writer.close()


def test_prune_cancels_expired_before_close_and_deletes_deleted_legacy_rule_events(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        legacy_rule_id = add_rule(
            connection,
            type="drawdown_from_high",
            symbol="510300",
            name="Legacy",
            asset_type="cn_etf",
            params={"lookback_days": 30, "thresholds": [0.1]},
        )
        old_event_id = add_alert_event(
            connection,
            rule_id=legacy_rule_id,
            alert_key="510300:drawdown:30:peak:2024-01-01:threshold:0.1",
            title="Drawdown reminder",
            message="old",
            payload={"peak_date": "2024-01-01"},
        )
        connection.execute(
            "UPDATE alert_events SET notification_status = 'sent', "
            "triggered_at = '2024-01-02T00:00:00+00:00' WHERE id = ?",
            (old_event_id,),
        )
        connection.commit()
        delete_rule(connection, legacy_rule_id)

        pre_event_id = add_alert_event(
            connection,
            rule_id=99,
            alert_key="99:drawdown_plan:pre_alert:2026-01-01",
            title="Pre-alert",
            message="pre",
            payload={"phase": "before_close", "data_date": "2026-01-01"},
        )
        ensure_notification_delivery_targets(
            connection,
            event_ids=(pre_event_id,),
            targets=(("telegram:123", "telegram"),),
        )
        connection.execute(
            "UPDATE alert_events SET notification_status = ? WHERE id = ?",
            (ALERT_NOTIFICATION_FAILED, pre_event_id),
        )
        connection.execute(
            "UPDATE notification_deliveries SET status = 'failed' WHERE event_id = ?",
            (pre_event_id,),
        )
        connection.commit()

        counts = prune_database(connection, today=date(2026, 1, 2))
        assert counts["alert_events"] == 1
        pre_row = connection.execute(
            "SELECT notification_status FROM alert_events WHERE id = ?",
            (pre_event_id,),
        ).fetchone()
        del_row = connection.execute(
            "SELECT status FROM notification_deliveries WHERE event_id = ?",
            (pre_event_id,),
        ).fetchone()
    assert pre_row["notification_status"] == ALERT_NOTIFICATION_CANCELLED
    assert del_row["status"] == "cancelled"


def test_reconcile_position_snapshot_preserves_sync_required_without_fund_settings(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        rule_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="CSI300",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "sma_window": 250,
                "sma_slope_window": 20,
                "rearm_margin": 0.02,
                "tiers": [{"drawdown": 0.1, "amount": 1000}],
            },
        )
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        history = pd.DataFrame(
            {
                "date": pd.to_datetime([date(2024, 1, 1), date(2024, 1, 2)]),
                "open": [100.0, 85.0],
                "high": [100.0, 85.0],
                "low": [100.0, 85.0],
                "close": [100.0, 85.0],
                "volume": [1000.0, 1000.0],
                "amount": [10000.0, 10000.0],
                "source": ["akshare_eastmoney", "akshare_eastmoney"],
            }
        )
        history.attrs.update(
            {
                "symbol": "510300",
                "source": "akshare_eastmoney",
                "price_basis": "qfq",
                "frequency": "daily",
            }
        )
        eval_res = checks.evaluate_drawdown_plan_rule(
            connection, rule, history, expected_date=date(2024, 1, 2)
        )
        assert eval_res.notification is not None
        record_manual_addition(
            connection,
            rule_id=rule_id,
            cycle_id=eval_res.cycle_id,
            source_alert_event_id=eval_res.notification.event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(drawdown=0.1, amount=1000, key="0.1"),),
            action_at=datetime(2024, 1, 2, 14, 0, tzinfo=UTC),
            create_estimate=False,
        )
        # Ensure fund_settings row is absent to test UPSERT behavior.
        connection.execute("DELETE FROM fund_settings WHERE fund_symbol = '000001'")
        connection.commit()

        pending = list_pending_position_items(connection, "000001")
        reconcile_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100.0,
            average_unit_cost=1.2,
            expected_item_keys=tuple(str(item["key"]) for item in pending),
            all_included=False,
            synced_at=datetime(2024, 1, 3, 10, 0, tzinfo=UTC),
        )
        snapshot = get_position_snapshot(connection, "000001")
        settings = get_fund_settings(connection, "000001")
    assert snapshot is not None
    assert snapshot["position_sync_required_since"] is not None
    assert settings is not None
    assert settings["position_sync_required_since"] is not None


def test_http_delivery_rejects_redirects_and_ntfy_sanitizes_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_redirect(url: str, **kwargs: object) -> SimpleNamespace:
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=302)

    monkeypatch.setattr(
        "fund_alert_bot.notifications.http_delivery.requests.post",
        fake_redirect,
    )
    res = post_notification("webhook", "https://example.test/hook", timeout=5)
    assert res.success is False
    assert res.detail == "http_status=302"
    assert calls[0]["allow_redirects"] is False

    calls.clear()

    def fake_ok(url: str, **kwargs: object) -> SimpleNamespace:
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        "fund_alert_bot.notifications.http_delivery.requests.post",
        fake_ok,
    )
    channel = NtfyNotificationChannel(
        server_url="https://ntfy.example.test",
        topic="my topic?x=1",
    )
    ntfy_res = asyncio.run(
        channel.send(NotificationMessage(title="Line1\r\nLine2", body="Body"))
    )
    assert ntfy_res.success is True
    assert calls[0]["url"] == "https://ntfy.example.test/my%20topic%3Fx%3D1"
    assert calls[0]["headers"] == {"Title": "Line1 Line2"}


def test_valuation_cache_preserved_when_refresh_fails_on_earlier_clock(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    quote = IndexValuation(
        index_symbol="000300",
        name="沪深300",
        date=date(2024, 1, 5),
        pe1=12.0,
        pe2=11.5,
        dividend_yield1=2.5,
        dividend_yield2=2.6,
    )

    class FailingProvider:
        def get_index_valuations(self, index_symbol: str) -> tuple[IndexValuation, ...]:
            del index_symbol
            raise RuntimeError("network down")

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        connection.execute(
            """
            INSERT INTO index_valuation_cache
                (index_symbol, observations_json, attempted_at,
                 succeeded_at, refresh_failed)
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                "000300",
                json.dumps([quote.payload()]),
                datetime(2024, 1, 5, 10, 0, tzinfo=UTC).isoformat(),
                datetime(2024, 1, 5, 10, 0, tzinfo=UTC).isoformat(),
            ),
        )
        connection.commit()

        # Simulate clock step-back earlier than cached quote date + refresh failure.
        get_valuation_context(
            connection,
            FailingProvider(),
            "000300",
            as_of_date=date(2024, 1, 4),
            now=datetime(2024, 1, 4, 10, 0, tzinfo=UTC),
        )
        row = connection.execute(
            "SELECT observations_json, refresh_failed FROM index_valuation_cache "
            "WHERE index_symbol = '000300'"
        ).fetchone()
    assert row["observations_json"] is not None
    assert row["refresh_failed"] == 1


def test_i18n_fixes_indented_status_and_position_lines() -> None:
    prev = i18n.get_language()
    try:
        i18n.set_language("zh-CN")
        assert i18n.localize_text("  Last started: 2026-01-01 08:00:00") == (
            "  最近开始： 2026-01-01 08:00:00"
        )
        assert i18n.localize_text("  Last finished: 2026-01-01 08:01:00") == (
            "  最近结束： 2026-01-01 08:01:00"
        )
        assert i18n.localize_text("  Last complete success: 2026-01-01 08:01:00") == (
            "  最近完整成功： 2026-01-01 08:01:00"
        )
        assert (
            i18n.localize_text("Position cycle unavailable — rerun /sync_position")
            == "持仓周期不可用——请重新运行 /sync_position"
        )
        assert (
            i18n.localize_text(
                "Position: exact; last sync 2026-01-01; later estimates 2"
            )
            == "持仓： 精确；最近同步 2026-01-01；后续估算 2"
        )
        assert i18n.localize_text("Plan: CSI300 (1)") == "计划： CSI300 (1)"

        i18n.set_language("en")
        assert i18n.localize_text("• 计划金额：1000") == "• Planned amount: 1000"
    finally:
        i18n.set_language(prev)


def test_edit_message_text_ignores_not_modified_and_splits_long_text() -> None:
    class NotModifiedQuery:
        async def edit_message_text(self, text: str, reply_markup: Any = None) -> None:
            del text, reply_markup
            raise RuntimeError("BadRequest: Message is not modified")

    asyncio.run(commands._edit_message_text(NotModifiedQuery(), "same"))

    replied: list[str] = []
    edited: list[str] = []

    class LongMessageQuery:
        message = SimpleNamespace(
            reply_text=lambda chunk, **kw: _append(replied, chunk)
        )

        async def edit_message_text(self, text: str, reply_markup: Any = None) -> None:
            del reply_markup
            edited.append(text)

    async def _append(target: list[str], value: str) -> None:
        target.append(value)

    long_text = "A" * 5000
    asyncio.run(commands._edit_message_text(LongMessageQuery(), long_text))
    assert len(edited) == 1 and len(edited[0]) == 4096
    assert len(replied) == 1 and len(replied[0]) == 904


def test_same_day_drawdown_plan_retry_restores_telegram_actions(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        rule_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="CSI300",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "sma_window": 250,
                "sma_slope_window": 20,
                "rearm_margin": 0.02,
                "tiers": [{"drawdown": 0.1, "amount": 1000}],
            },
        )
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        history = pd.DataFrame(
            {
                "date": pd.to_datetime([date(2024, 1, 1), date(2024, 1, 2)]),
                "open": [100.0, 85.0],
                "high": [100.0, 85.0],
                "low": [100.0, 85.0],
                "close": [100.0, 85.0],
                "volume": [1000.0, 1000.0],
                "amount": [10000.0, 10000.0],
                "source": ["akshare_eastmoney", "akshare_eastmoney"],
            }
        )
        history.attrs.update(
            {
                "symbol": "510300",
                "source": "akshare_eastmoney",
                "price_basis": "qfq",
                "frequency": "daily",
            }
        )
        checks.evaluate_drawdown_plan_rule(
            connection, rule, history, expected_date=date(2024, 1, 2)
        )

    sent: list[dict[str, Any]] = []

    async def send_message(**kwargs: Any) -> None:
        sent.append(kwargs)

    app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    retried = asyncio.run(
        scheduler.retry_pending_drawdown_plan_notifications(
            application=app,
            sqlite_path=sqlite_path,
            allowed_user_ids={123},
            action_date=date(2024, 1, 2),
        )
    )
    assert retried == 1
    assert len(sent) == 1
    assert "reply_markup" in sent[0]


def test_plan_views_skips_malformed_dca_row() -> None:
    text = plan_views.format_plan_overview(
        checks.DrawdownPlanStatusResult(0, [], [], []),
        dca_statuses=[
            {
                "rule_id": 1,
                "name": "Bad DCA",
                "fund_symbol": "000001",
                "params_json": "{broken",
            }
        ],
    )
    assert "📊 Investment Plans" in text


def test_register_jobs_sets_heartbeat_and_before_close_grace_times(
    tmp_path: Path,
) -> None:
    jobs: dict[str, dict[str, Any]] = {}

    class RecordingScheduler:
        def add_job(self, func: Any, **kwargs: Any) -> None:
            del func
            jobs[str(kwargs["id"])] = kwargs

    scheduler.register_jobs(
        RecordingScheduler(),
        application=SimpleNamespace(bot=object()),
        sqlite_path=tmp_path / "bot.sqlite3",
        allowed_user_ids={123},
        timezone="Asia/Shanghai",
    )
    assert jobs[scheduler.HEARTBEAT_JOB_ID]["misfire_grace_time"] == 50
    assert jobs[scheduler.MARKET_BEFORE_CLOSE_JOB_ID]["misfire_grace_time"] == 600
