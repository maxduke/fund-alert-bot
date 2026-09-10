"""Bounded diagnostics derived from local pending work, without evaluating it."""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fund_alert_bot.i18n import localize_text
from fund_alert_bot.rules.dca import rule_creation_date

DETAIL_LIMIT = 3


def _date_text(value: str | None) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return localize_text("Unknown date")


def _timestamp(value: str, zone: ZoneInfo) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return localize_text("Unknown date")


def _reason(row: sqlite3.Row, *, today: date, zone: ZoneInfo) -> str:
    """Report a known prerequisite, never a guessed last processor failure."""

    if row["kind"] == "dca":
        try:
            created = rule_creation_date({"created_at": row["rule_created_at"]}, zone)
            if date.fromisoformat(row["original_date"]) < created:
                return "Predates rule creation; reconcile explicitly."
        except (ValueError, TypeError):
            return "Reason unknown from local records."
    if row["effective_date"] is None:
        return "Effective trading date not yet recorded."
    try:
        if date.fromisoformat(row["effective_date"]) >= today:
            return "Effective date has not completed."
    except ValueError:
        return "Reason unknown from local records."
    if row["position_fund"] is None:
        return "Initial position is missing; position sync required."
    if row["position_sync"] is not None or row["settings_sync"] is not None:
        return "Position reconciliation is required."
    nav = row["unit_nav"]
    if (
        nav is None
        or not math.isfinite(nav)
        or nav <= 0
        or row["nav_source"] != "akshare_eastmoney"
    ):
        return "Exact-date NAV unavailable locally; remote publication unknown."
    return "Reason unknown from local records."


def _estimate_lines(
    connection: sqlite3.Connection, *, zone: ZoneInfo, today: date
) -> list[str]:
    lines = []
    for table, kind, original, heading in (
        (
            "scheduled_dca_occurrences",
            "dca",
            "due_date",
            "Oldest pending DCA estimates:",
        ),
        ("manual_add_estimates", "manual", "action_date", "Oldest pending additions:"),
    ):
        # Identifiers above are fixed internal constants; no user input is SQL.
        rows = connection.execute(
            f"""
            SELECT e.id, e.rule_id, e.fund_symbol, e.{original} AS original_date,
                   e.effective_date, e.created_at, ? AS kind,
                   r.created_at AS rule_created_at,
                   p.fund_symbol AS position_fund,
                   p.position_sync_required_since AS position_sync,
                   s.position_sync_required_since AS settings_sync,
                   n.unit_nav, n.source AS nav_source
            FROM {table} AS e
            LEFT JOIN rules AS r ON r.id = e.rule_id
            LEFT JOIN position_snapshots AS p ON p.fund_symbol = e.fund_symbol
            LEFT JOIN fund_settings AS s ON s.fund_symbol = e.fund_symbol
            LEFT JOIN fund_nav_history AS n ON n.fund_symbol = e.fund_symbol
                AND n.nav_date = e.effective_date
            WHERE e.status = 'pending'
            ORDER BY julianday(e.created_at), e.id
            LIMIT ?
            """,
            (kind, DETAIL_LIMIT + 1),
        ).fetchall()
        if not rows:
            continue
        lines.extend(("", localize_text(heading)))
        for row in rows[:DETAIL_LIMIT]:
            symbol = str(row["fund_symbol"])
            # Do not let malformed historical identifiers expand the reply.
            symbol = (
                symbol
                if len(symbol) == 6 and symbol.isascii() and symbol.isdigit()
                else "?"
            )
            lines.append(
                f"• #{row['id']} / {localize_text('Rule')} {row['rule_id']} / {symbol}"
            )
            lines.append(
                f"  {localize_text('Original date:')} "
                f"{_date_text(row['original_date'])}; "
                f"{localize_text('Effective date:')} "
                f"{_date_text(row['effective_date'])}"
            )
            lines.append(
                f"  {localize_text('Pending since:')} "
                f"{_timestamp(row['created_at'], zone)}"
            )
            reason = _reason(row, today=today, zone=zone)
            lines.append("  " + localize_text(reason))
            if reason in {
                "Initial position is missing; position sync required.",
                "Position reconciliation is required.",
                "Predates rule creation; reconcile explicitly.",
            }:
                lines.append(
                    f"  {localize_text('Review with:')} /sync_position {symbol}"
                )
                if reason == "Predates rule creation; reconcile explicitly.":
                    lines.append(
                        f"  {localize_text('If not executed:')} /dca_skip "
                        f"{row['rule_id']} {_date_text(row['original_date'])}"
                    )
        if len(rows) > DETAIL_LIMIT:
            lines.append(localize_text("Additional pending items omitted."))
    return lines


def format_pending_work(
    connection: sqlite3.Connection, *, zone: ZoneInfo, today: date
) -> list[str]:
    """Read within the caller's snapshot; never expose delivery payloads/targets."""

    lines = _estimate_lines(connection, zone=zone, today=today)
    rows = connection.execute(
        """
        SELECT * FROM (
        SELECT d.event_id, e.rule_id, d.status, d.created_at
        FROM notification_deliveries AS d
        JOIN alert_events AS e ON e.id = d.event_id
        WHERE d.status IN ('pending', 'sending', 'failed')
        UNION ALL
        SELECT e.id, e.rule_id, 'unassigned', e.triggered_at
        FROM alert_events AS e
        WHERE e.notification_status IN ('pending', 'failed')
            AND NOT EXISTS (
                SELECT 1 FROM notification_deliveries AS d WHERE d.event_id = e.id
            )
        ) ORDER BY julianday(created_at), event_id, status
        LIMIT ?
        """,
        (DETAIL_LIMIT + 1,),
    ).fetchall()
    if rows:
        lines.extend(("", localize_text("Oldest unfinished deliveries:")))
        statuses = {
            "pending": "Awaiting delivery",
            "sending": "Delivery in flight",
            "failed": "Delivery failed",
            "unassigned": "Delivery targets not yet assigned",
        }
        for row in rows[:DETAIL_LIMIT]:
            lines.append(
                f"• {localize_text('Event')} {row['event_id']} / "
                f"{localize_text('Rule')} {row['rule_id']}: "
                + localize_text(statuses[row["status"]])
            )
            lines.append(
                f"  {localize_text('Pending since:')} "
                f"{_timestamp(row['created_at'], zone)}"
            )
        if len(rows) > DETAIL_LIMIT:
            lines.append(localize_text("Additional pending items omitted."))
    if lines:
        lines.extend(
            (
                "",
                localize_text(
                    "Local prerequisites only; not a recorded failure diagnosis."
                ),
                localize_text("Command hints are incomplete; use /help for arguments."),
            )
        )
    return lines
