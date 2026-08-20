"""Notification dispatch helpers that persist delivery outcomes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fund_alert_bot.checks import AlertNotification
from fund_alert_bot.db import (
    ALERT_NOTIFICATION_SENT,
    claim_notification_deliveries,
    complete_notification_delivery,
    ensure_notification_delivery_targets,
    initialize_database,
    open_connection,
    refresh_alert_notification_status,
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
    """Send alert notifications with durable per-target leases and retries."""

    if not notifications:
        return NotificationDispatchSummary(attempted=0, delivered=0, failed=0)

    notification_by_event = {item.event_id: item for item in notifications}
    delivery_targets = notification_service.delivery_targets
    event_ids = tuple(notification_by_event)
    for batch in _notification_batches(notifications):
        batch_ids = tuple(item.event_id for item in batch)
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            ensure_notification_delivery_targets(
                connection,
                event_ids=batch_ids,
                targets=delivery_targets,
            )
        claimed_target_keys: list[str] = []
        for target_key, _channel in delivery_targets:
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                target_claims = claim_notification_deliveries(
                    connection,
                    event_ids=batch_ids,
                    target_keys=(target_key,),
                )
            if not target_claims:
                continue
            claimed_target_keys.append(target_key)
            notification = _merge_dca_batch(
                [notification_by_event[claim.event_id] for claim in target_claims]
            )
            result = await notification_service.send_target(
                target_key,
                title=notification.title,
                body=notification.text,
                telegram_actions=notification.telegram_actions,
            )
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                for claim in target_claims:
                    complete_notification_delivery(
                        connection,
                        event_id=claim.event_id,
                        target_key=claim.target_key,
                        claim_token=claim.claim_token,
                        result=result,
                    )
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            refresh_alert_notification_status(connection, event_ids=batch_ids)
        LOGGER.info(
            "Notification result event_ids=%s targets=%s",
            [item.event_id for item in batch],
            claimed_target_keys,
        )

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        status_rows = connection.execute(
            """
            SELECT id, notification_status
            FROM alert_events
            WHERE id IN ({})
            """.format(", ".join("?" for _ in event_ids)),
            event_ids,
        ).fetchall()
    delivered = sum(
        str(row["notification_status"]) == ALERT_NOTIFICATION_SENT
        for row in status_rows
    )
    failed = len(notifications) - delivered

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
    summary = batch[0].dca_summary
    if summary is None:
        return batch[0]
    if len(batch) == 1:
        if summary.rebuilt_text is None:
            return batch[0]
        return AlertNotification(
            event_id=batch[0].event_id,
            title=batch[0].title,
            text=summary.rebuilt_text,
            telegram_actions=batch[0].telegram_actions,
        )

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
