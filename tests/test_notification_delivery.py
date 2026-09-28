from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from fund_alert_bot.checks import AlertNotification, DcaNotificationSummary
from fund_alert_bot.db import (
    ALERT_NOTIFICATION_CANCELLED,
    ALERT_NOTIFICATION_SENT,
    NotificationDeliveryClaim,
    add_alert_event,
    cancel_removed_notification_targets,
    claim_notification_deliveries,
    complete_notification_delivery,
    ensure_notification_delivery_targets,
    initialize_database,
    open_connection,
    refresh_alert_notification_status,
    reserve_alert_event,
)
from fund_alert_bot.notifications import dispatch
from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.dispatch import (
    _common_delivery_progress,
    send_alert_notifications,
)
from fund_alert_bot.notifications.service import NotificationService
from fund_alert_bot.notifications.telegram import TelegramNotificationChannel


def test_telegram_splits_long_body_and_keeps_actions_on_last_chunk() -> None:
    calls: list[dict[str, object]] = []

    class RecordingBot:
        async def send_message(self, **kwargs: object) -> None:
            calls.append(kwargs)

    body = "x" * 9000
    result = asyncio.run(
        TelegramNotificationChannel(bot=RecordingBot(), chat_ids=(123,)).send_to(
            "telegram:123",
            NotificationMessage(
                title="Reminder",
                body=body,
                telegram_actions=((("Confirm", "confirm:1"),),),
            ),
        )
    )

    assert result.success is True
    assert "".join(str(call["text"]) for call in calls) == body
    assert all(len(str(call["text"])) <= 4096 for call in calls)
    assert "reply_markup" not in calls[0]
    assert "reply_markup" in calls[-1]


def test_telegram_target_fails_when_one_chunk_send_raises() -> None:
    calls: list[dict[str, object]] = []

    class FailingBot:
        async def send_message(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if len(calls) == 2:
                raise RuntimeError("temporary Telegram failure")

    result = asyncio.run(
        TelegramNotificationChannel(bot=FailingBot(), chat_ids=(123,)).send_to(
            "telegram:123",
            NotificationMessage(title="Reminder", body="x" * 9000),
        )
    )

    assert result.success is False
    assert result.detail == "unexpected_error=RuntimeError"
    assert len(calls) == 2
    assert result.sent_chunks == 1
    assert (
        result.body_fingerprint
        == hashlib.sha256(("x" * 9000).encode("utf-8")).hexdigest()
    )


def test_telegram_retry_resumes_after_failed_chunk_without_duplicate_chunks(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "telegram-chunk-retry")

    body = "A" * 4096 + "B" * 4096 + "C"
    bot = FailingChunkBot()
    service = NotificationService(
        [TelegramNotificationChannel(bot=bot, chat_ids=(123,))]
    )
    notification = _notification(event_id, body)

    first = asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=service,
            notifications=[notification],
        )
    )

    with open_connection(sqlite_path) as connection:
        result_json = connection.execute(
            "SELECT result_json FROM notification_deliveries WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
    assert first.failed == 1
    assert json.loads(result_json)["sent_chunks"] == 1

    second = asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=service,
            notifications=[notification],
        )
    )

    assert second.delivered == 1
    assert bot.calls == ["A" * 4096, "B" * 4096, "B" * 4096, "C"]


def test_merged_claim_progress_requires_consistent_metadata() -> None:
    claims = [
        NotificationDeliveryClaim(1, "telegram:1", "telegram", "one", 1, "a" * 64),
        NotificationDeliveryClaim(2, "telegram:1", "telegram", "two", 1, "a" * 64),
    ]
    assert _common_delivery_progress(claims) == (1, "a" * 64)
    claims[-1] = NotificationDeliveryClaim(
        2, "telegram:1", "telegram", "two", 2, "a" * 64
    )
    assert _common_delivery_progress(claims) is None


def test_concurrent_dispatch_claims_each_target_once(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "concurrent")

    channel = BlockingChannel()
    service = NotificationService([channel])

    async def run() -> None:
        first = asyncio.create_task(
            send_alert_notifications(
                sqlite_path=sqlite_path,
                notification_service=service,
                notifications=[_notification(event_id, "concurrent")],
            )
        )
        await channel.started.wait()
        second = asyncio.create_task(
            send_alert_notifications(
                sqlite_path=sqlite_path,
                notification_service=service,
                notifications=[_notification(event_id, "concurrent")],
            )
        )
        await asyncio.sleep(0)
        channel.release.set()
        await asyncio.gather(first, second)

    asyncio.run(run())

    assert channel.messages == ["concurrent"]
    assert _status(sqlite_path, event_id) == ALERT_NOTIFICATION_SENT


def test_failed_target_retries_without_resending_successful_target(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "partial")

    channel = PartialFailureChannel()
    service = NotificationService([channel])
    notification = _notification(event_id, "partial")

    first = asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=service,
            notifications=[notification],
        )
    )
    second = asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=service,
            notifications=[notification],
        )
    )

    assert first.failed == 1
    assert second.delivered == 1
    assert channel.calls == ["target:a", "target:b", "target:b"]
    assert _status(sqlite_path, event_id) == ALERT_NOTIFICATION_SENT


def test_telegram_retries_only_the_failed_chat(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "telegram-partial")

    bot = PartialFailureBot()
    service = NotificationService(
        [TelegramNotificationChannel(bot=bot, chat_ids=(101, 202))]
    )
    notification = _notification(event_id, "telegram-partial")

    asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=service,
            notifications=[notification],
        )
    )
    asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=service,
            notifications=[notification],
        )
    )

    assert bot.calls == [101, 202, 202]
    assert _status(sqlite_path, event_id) == ALERT_NOTIFICATION_SENT


def test_expired_delivery_lease_can_be_recovered(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "lease")
        ensure_notification_delivery_targets(
            connection,
            event_ids=[event_id],
            targets=[("bark", "bark")],
        )
        first = claim_notification_deliveries(
            connection,
            event_ids=[event_id],
            target_keys=["bark"],
        )[0]
        connection.execute(
            """
            UPDATE notification_deliveries
            SET claim_until = '2000-01-01T00:00:00+00:00'
            WHERE event_id = ? AND target_key = 'bark'
            """,
            (event_id,),
        )
        connection.commit()
        second = claim_notification_deliveries(
            connection,
            event_ids=[event_id],
            target_keys=["bark"],
        )[0]
        assert second.claim_token != first.claim_token
        assert not complete_notification_delivery(
            connection,
            event_id=event_id,
            target_key="bark",
            claim_token=first.claim_token,
            result=NotificationResult(channel="bark", success=True),
        )
        assert complete_notification_delivery(
            connection,
            event_id=event_id,
            target_key="bark",
            claim_token=second.claim_token,
            result=NotificationResult(channel="bark", success=True),
        )


def test_dca_batch_sends_once_per_target_and_records_each_event(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        first_id = _add_event(connection, "dca-1")
        second_id = _add_event(connection, "dca-2")

    channel = TargetRecordingChannel()
    service = NotificationService([channel])
    summary = asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=service,
            notifications=[
                _dca_notification(first_id, "A500", 100),
                _dca_notification(second_id, "ChiNext", 200),
            ],
        )
    )

    assert summary.delivered == 2
    assert channel.calls == ["target:a", "target:b"]
    assert all("Fixed DCA reminders" in body for body in channel.bodies)
    with open_connection(sqlite_path) as connection:
        rows = connection.execute(
            """
            SELECT event_id, target_key, status
            FROM notification_deliveries
            ORDER BY event_id, target_key
            """
        ).fetchall()
    assert [(row["event_id"], row["target_key"], row["status"]) for row in rows] == [
        (first_id, "target:a", "sent"),
        (first_id, "target:b", "sent"),
        (second_id, "target:a", "sent"),
        (second_id, "target:b", "sent"),
    ]


class BlockingChannel:
    name = "test"
    target_keys = ("test",)

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.messages: list[str] = []

    async def send(self, message: NotificationMessage) -> NotificationResult:
        self.messages.append(message.body)
        self.started.set()
        await self.release.wait()
        return NotificationResult(channel=self.name, success=True, detail="sent")


class PartialFailureChannel:
    name = "test"
    target_keys = ("target:a", "target:b")

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_to(
        self,
        target_key: str,
        message: NotificationMessage,
    ) -> NotificationResult:
        del message
        self.calls.append(target_key)
        return NotificationResult(
            channel=self.name,
            success=target_key == "target:a" or self.calls.count(target_key) > 1,
            detail=(
                "sent"
                if target_key == "target:a" or self.calls.count(target_key) > 1
                else "failed"
            ),
        )


class TargetRecordingChannel:
    name = "test"
    target_keys = ("target:a", "target:b")

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bodies: list[str] = []

    async def send_to(
        self,
        target_key: str,
        message: NotificationMessage,
    ) -> NotificationResult:
        self.calls.append(target_key)
        self.bodies.append(message.body)
        return NotificationResult(channel=self.name, success=True, detail="sent")


def test_targets_are_claimed_only_when_a_delivery_slot_is_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "slots")

    events: list[str] = []
    real_claim = dispatch.claim_notification_deliveries

    def recording_claim(connection, *, event_ids, target_keys):
        events.append(f"claim:{target_keys[0]}")
        return real_claim(connection, event_ids=event_ids, target_keys=target_keys)

    channel = PartialFailureChannel()
    real_send_to = channel.send_to

    async def recording_send_to(target_key, message):
        events.append(f"send:{target_key}")
        return await real_send_to(target_key, message)

    channel.send_to = recording_send_to
    monkeypatch.setattr(dispatch, "MAX_CONCURRENT_DELIVERIES", 1)
    monkeypatch.setattr(dispatch, "claim_notification_deliveries", recording_claim)

    asyncio.run(
        send_alert_notifications(
            sqlite_path=sqlite_path,
            notification_service=NotificationService([channel]),
            notifications=[_notification(event_id, "slots")],
        )
    )

    assert events == [
        "claim:target:a",
        "send:target:a",
        "claim:target:b",
        "send:target:b",
    ]


def test_stale_delivery_result_is_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sqlite_path = tmp_path / "alerts.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "stale")

    monkeypatch.setattr(
        dispatch,
        "complete_notification_delivery",
        lambda connection, **kwargs: False,
    )

    with caplog.at_level(logging.WARNING, logger=dispatch.__name__):
        asyncio.run(
            send_alert_notifications(
                sqlite_path=sqlite_path,
                notification_service=NotificationService([PartialFailureChannel()]),
                notifications=[_notification(event_id, "stale")],
            )
        )

    assert "Discarded stale notification result" in caplog.text
    assert f"event_id={event_id}" in caplog.text


class PartialFailureBot:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def send_message(self, *, chat_id: int, text: str, **kwargs) -> None:
        del text, kwargs
        self.calls.append(chat_id)
        if chat_id == 202 and self.calls.count(chat_id) == 1:
            raise RuntimeError("temporary Telegram failure")


class FailingChunkBot:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_message(self, *, text: str, **kwargs: object) -> None:
        del kwargs
        self.calls.append(text)
        if len(self.calls) == 2:
            raise RuntimeError("temporary Telegram failure")


def _add_event(connection, key: str) -> int:
    return add_alert_event(
        connection,
        rule_id=1,
        alert_key=key,
        title="Reminder",
        message=key,
    )


def _notification(event_id: int, text: str) -> AlertNotification:
    return AlertNotification(event_id=event_id, title="Reminder", text=text)


def _dca_notification(event_id: int, fund: str, amount: float) -> AlertNotification:
    return AlertNotification(
        event_id=event_id,
        title="DCA reminder",
        text=f"DCA {fund}",
        dca_summary=DcaNotificationSummary(
            due_date="2026-08-20",
            lines=(f"Fund: {fund}", f"Amount: {amount}"),
            amount=amount,
            skipped=False,
        ),
    )


def _status(sqlite_path: Path, event_id: int) -> str:
    with open_connection(sqlite_path) as connection:
        return str(
            connection.execute(
                "SELECT notification_status FROM alert_events WHERE id = ?",
                (event_id,),
            ).fetchone()[0]
        )


def test_removed_target_cancellation_keeps_success_and_prior_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "removed-partial")
        ensure_notification_delivery_targets(
            connection,
            event_ids=[event_id],
            targets=[("active", "test"), ("removed", "test")],
        )
        claims = claim_notification_deliveries(connection, event_ids=[event_id])
        for claim in claims:
            complete_notification_delivery(
                connection,
                event_id=event_id,
                target_key=claim.target_key,
                claim_token=claim.claim_token,
                result=NotificationResult(
                    channel="test",
                    success=claim.target_key == "active",
                    detail="network_error" if claim.target_key == "removed" else "",
                ),
            )
        assert (
            cancel_removed_notification_targets(
                connection, active_target_keys=["active"]
            )
            == 1
        )
        assert (
            cancel_removed_notification_targets(
                connection, active_target_keys=["active"]
            )
            == 0
        )
        rows = connection.execute(
            "SELECT target_key, status, result_json FROM notification_deliveries "
            "WHERE event_id = ? ORDER BY target_key",
            (event_id,),
        ).fetchall()
        assert [(row["target_key"], row["status"]) for row in rows] == [
            ("active", "sent"),
            ("removed", "cancelled"),
        ]
        removed_result = json.loads(rows[1]["result_json"])
        assert removed_result["detail"] == "network_error"
        assert (
            removed_result["cancellation"]["reason"]
            == "target_removed_from_configuration"
        )
        assert removed_result["cancellation"]["at"]
        assert not claim_notification_deliveries(connection, event_ids=[event_id])
        ensure_notification_delivery_targets(
            connection,
            event_ids=[event_id],
            targets=[("active", "test"), ("removed", "test"), ("new", "test")],
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM notification_deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
            == 2
        )
    assert _status(path, event_id) == ALERT_NOTIFICATION_CANCELLED


def test_all_removed_targets_cancel_and_do_not_reopen_event(tmp_path: Path) -> None:
    path = tmp_path / "alerts.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "all-removed")
        ensure_notification_delivery_targets(
            connection, event_ids=[event_id], targets=[("removed", "test")]
        )
        assert (
            cancel_removed_notification_targets(connection, active_target_keys=[]) == 1
        )
        refresh_alert_notification_status(connection, event_ids=[event_id])
        assert (
            connection.execute(
                "SELECT notification_sent_at FROM alert_events WHERE id = ?",
                (event_id,),
            ).fetchone()[0]
            is None
        )
        with pytest.raises(sqlite3.IntegrityError):
            reserve_alert_event(
                connection,
                rule_id=1,
                alert_key="all-removed",
                title="Reminder",
                message="again",
            )
    assert _status(path, event_id) == ALERT_NOTIFICATION_CANCELLED

    summary = asyncio.run(
        send_alert_notifications(
            sqlite_path=path,
            notification_service=NotificationService([]),
            notifications=[_notification(event_id, "all removed")],
        )
    )
    assert (summary.delivered, summary.failed, summary.cancelled) == (0, 0, 1)


def test_cancellation_keeps_active_failed_target_retryable_and_revokes_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "mixed-removal")
        ensure_notification_delivery_targets(
            connection,
            event_ids=[event_id],
            targets=[("active", "test"), ("removed", "test")],
        )
        claims = claim_notification_deliveries(connection, event_ids=[event_id])
        removed_claim = next(c for c in claims if c.target_key == "removed")
        active_claim = next(c for c in claims if c.target_key == "active")
        complete_notification_delivery(
            connection,
            event_id=event_id,
            target_key="active",
            claim_token=active_claim.claim_token,
            result=NotificationResult(channel="test", success=False, detail="offline"),
        )
        assert (
            cancel_removed_notification_targets(
                connection, active_target_keys=["active"]
            )
            == 1
        )
        assert not complete_notification_delivery(
            connection,
            event_id=event_id,
            target_key="removed",
            claim_token=removed_claim.claim_token,
            result=NotificationResult(channel="test", success=True),
        )
        retry = claim_notification_deliveries(connection, event_ids=[event_id])
        assert [claim.target_key for claim in retry] == ["active"]
        complete_notification_delivery(
            connection,
            event_id=event_id,
            target_key="active",
            claim_token=retry[0].claim_token,
            result=NotificationResult(channel="test", success=True),
        )
    assert _status(path, event_id) == ALERT_NOTIFICATION_CANCELLED


def test_old_delivery_constraint_migrates_without_losing_state_or_indexes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        event_id = _add_event(connection, "legacy-check")
        connection.execute("DROP TABLE notification_deliveries")
        connection.execute(
            """
            CREATE TABLE notification_deliveries (
                event_id INTEGER NOT NULL REFERENCES alert_events(id) ON DELETE CASCADE,
                target_key TEXT NOT NULL, channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending', 'sending', 'sent', 'failed')
                ),
                claim_token TEXT, claim_until TEXT, attempted_at TEXT, sent_at TEXT,
                result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (event_id, target_key)
            )
            """
        )
        connection.execute(
            "CREATE INDEX notification_deliveries_claim_lookup "
            "ON notification_deliveries(status, claim_until, event_id)"
        )
        connection.execute(
            "CREATE INDEX legacy_delivery_test_index "
            "ON notification_deliveries(channel)"
        )
        connection.execute(
            "INSERT INTO notification_deliveries VALUES "
            "(?, 'removed', 'test', 'sending', 'token', '2026-01-01', "
            "'2026-01-01', NULL, ?, '2026-01-01', '2026-01-01')",
            (event_id, '{"detail":"prior failure"}'),
        )
        connection.commit()
        initialize_database(connection)
        initialize_database(connection)
        row = connection.execute(
            "SELECT * FROM notification_deliveries WHERE event_id = ?", (event_id,)
        ).fetchone()
        assert row["claim_token"] == "token"
        assert json.loads(row["result_json"])["detail"] == "prior failure"
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'notification_deliveries'"
            )
        } >= {"notification_deliveries_claim_lookup", "legacy_delivery_test_index"}
        assert (
            cancel_removed_notification_targets(connection, active_target_keys=[]) == 1
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
