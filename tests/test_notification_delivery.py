from __future__ import annotations

import asyncio
from pathlib import Path

from fund_alert_bot.checks import AlertNotification, DcaNotificationSummary
from fund_alert_bot.db import (
    ALERT_NOTIFICATION_SENT,
    add_alert_event,
    claim_notification_deliveries,
    complete_notification_delivery,
    ensure_notification_delivery_targets,
    initialize_database,
    open_connection,
)
from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.dispatch import send_alert_notifications
from fund_alert_bot.notifications.service import NotificationService
from fund_alert_bot.notifications.telegram import TelegramNotificationChannel


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


class PartialFailureBot:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def send_message(self, *, chat_id: int, text: str, **kwargs) -> None:
        del text, kwargs
        self.calls.append(chat_id)
        if chat_id == 202 and self.calls.count(chat_id) == 1:
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
