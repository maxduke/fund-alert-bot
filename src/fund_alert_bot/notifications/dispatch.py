"""Notification dispatch helpers that persist delivery outcomes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fund_alert_bot.checks import AlertNotification
from fund_alert_bot.db import (
    initialize_database,
    open_connection,
    record_alert_notification_result,
)
from fund_alert_bot.notifications.service import NotificationService
from fund_alert_bot.rules.dca import format_dca_amount

LOGGER = logging.getLogger(__name__)
TELEGRAM_TEXT_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class NotificationDispatchSummary:
    """Summary of alert notification delivery attempts."""

    attempted: int
    delivered: int
    failed: int


async def send_alert_notifications(
    *,
    sqlite_path: str | Path,
    notification_service: NotificationService,
    notifications: list[AlertNotification],
) -> NotificationDispatchSummary:
    """Send alert notifications and record channel delivery results."""

    delivered = 0
    failed = 0
    for batch in _notification_batches(notifications):
        notification = _merge_dca_batch(batch)
        results = await notification_service.send_alert(
            title=notification.title,
            body=notification.text,
            telegram_actions=notification.telegram_actions,
        )
        if any(result.success for result in results):
            delivered += len(batch)
        else:
            failed += len(batch)

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            for item in batch:
                record_alert_notification_result(
                    connection,
                    event_id=item.event_id,
                    results=results,
                )
        LOGGER.info(
            "Notification result event_ids=%s channels=%s",
            [item.event_id for item in batch],
            [(result.channel, result.success) for result in results],
        )

    return NotificationDispatchSummary(
        attempted=len(notifications),
        delivered=delivered,
        failed=failed,
    )


def _notification_batches(
    notifications: list[AlertNotification],
) -> list[list[AlertNotification]]:
    batches: list[list[AlertNotification]] = []
    dca_indexes: dict[str, int] = {}
    for notification in notifications:
        summary = notification.dca_summary
        if summary is None:
            batches.append([notification])
            continue
        index = dca_indexes.get(summary.due_date)
        if index is None:
            dca_indexes[summary.due_date] = len(batches)
            batches.append([notification])
        elif len(_merge_dca_batch([*batches[index], notification]).text) > (
            TELEGRAM_TEXT_LIMIT
        ):
            dca_indexes[summary.due_date] = len(batches)
            batches.append([notification])
        else:
            batches[index].append(notification)
    return batches


def _merge_dca_batch(batch: list[AlertNotification]) -> AlertNotification:
    if len(batch) == 1 or batch[0].dca_summary is None:
        return batch[0]

    summaries = [item.dca_summary for item in batch]
    due_date = summaries[0].due_date
    lines = ["💰 Fixed DCA reminders", "", f"Scheduled date: {due_date}", ""]
    for summary in summaries:
        lines.extend((*summary.lines, ""))
    total = sum(summary.amount for summary in summaries if not summary.skipped)
    lines.extend(
        (
            f"Total planned amount: {format_dca_amount(total)} RMB",
            "",
            "The bot assumes the configured deduction executes; it does not verify it.",
            "Remember to run /sync_position after any visible platform mismatch.",
            "Reminder only. No trade has been placed.",
        )
    )
    return AlertNotification(
        event_id=batch[0].event_id,
        title="DCA reminders",
        text="\n".join(lines),
        telegram_actions=tuple(
            row for notification in batch for row in notification.telegram_actions
        ),
    )
