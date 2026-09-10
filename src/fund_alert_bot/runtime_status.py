"""Local task outcomes and a read-only operational summary."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fund_alert_bot.db import initialize_database, open_connection
from fund_alert_bot.i18n import localize_text
from fund_alert_bot.pending_status import format_pending_work

LOGGER = logging.getLogger(__name__)
_PROCESS_ID = uuid4().hex
_KEY_PREFIX = "job_status:"
JOB_LABELS = {
    "market-before-close-check": "Before-close check",
    "market-after-close-check": "After-close check",
    "dca-morning-reminder-check": "DCA check",
    "fund-nav-process": "Fund NAV processing",
}
_OUTCOMES = {
    "running": "In progress",
    "ok": "Completed successfully",
    "partial": "Incomplete (data or delivery problems)",
    "failed": "Execution failed",
    "skipped": "No evaluation due",
    "interrupted": "Interrupted; no completion recorded",
}


@dataclass
class _Run:
    outcome: str = "ok"
    no_data: int = 0
    errors: int = 0
    delivery_failures: int = 0


_CURRENT_RUN: ContextVar[_Run | None] = ContextVar("job_status_run", default=None)


def observe_job_result(
    *,
    no_data: int = 0,
    errors: int = 0,
    delivery_failures: int = 0,
    skipped: bool = False,
) -> None:
    """Accumulate results, including rule errors handled without an exception."""

    run = _CURRENT_RUN.get()
    if run is None:
        return
    run.no_data += no_data
    run.errors += errors
    run.delivery_failures += delivery_failures
    if run.no_data or run.errors or run.delivery_failures:
        run.outcome = "partial"
    elif skipped:
        run.outcome = "skipped"


def _persist_run(
    sqlite_path: str | Path,
    job_id: str,
    run_id: str,
    result: _Run | None,
) -> None:
    """Keep one bounded record per job; an older run cannot finish a newer one."""

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        connection.execute("BEGIN IMMEDIATE")
        key = _KEY_PREFIX + job_id
        row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = ?", (key,)
        ).fetchone()
        state = {} if row is None else json.loads(row["value"])
        now = datetime.now(UTC).isoformat()
        if result is None:
            state.update(
                run_id=run_id,
                process_id=_PROCESS_ID,
                started_at=now,
                finished_at=None,
                outcome="running",
                no_data=0,
                errors=0,
                delivery_failures=0,
            )
        elif state.get("run_id") != run_id:
            return
        else:
            state.update(
                finished_at=now,
                outcome=result.outcome,
                no_data=result.no_data,
                errors=result.errors,
                delivery_failures=result.delivery_failures,
            )
            if result.outcome == "ok":
                state["last_success_at"] = now
        connection.execute(
            """
            INSERT INTO app_metadata (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(state), now),
        )
        connection.commit()


async def _save_run(*args: Any) -> None:
    try:
        await asyncio.to_thread(_persist_run, *args)
    except Exception:
        # Observability must not prevent a reminder check from running.
        LOGGER.exception("Could not save task status")


def track_job(
    job_id: str,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Record scheduled and startup executions without changing their return values."""

    def decorate(work: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(work)
        async def tracked(**kwargs: Any) -> Any:
            run_id = uuid4().hex
            path = kwargs["sqlite_path"]
            run = _Run()
            token = _CURRENT_RUN.set(run)
            try:
                await _save_run(path, job_id, run_id, None)
                try:
                    return await work(**kwargs)
                except asyncio.CancelledError:
                    run.outcome = "interrupted"
                    raise
                except Exception:
                    run.outcome = "failed"
                    run.errors += 1
                    raise
                finally:
                    await _save_run(path, job_id, run_id, run)
            finally:
                _CURRENT_RUN.reset(token)

        return tracked

    return decorate


def format_runtime_status(sqlite_path: str | Path, *, timezone: str) -> str:
    """Read a local snapshot; never contact providers or evaluate reminders."""

    zone = ZoneInfo(timezone)

    def timestamp(value: str | None) -> str:
        if not value:
            return localize_text("No record yet")
        return (
            datetime.fromisoformat(value).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
        )

    lines = ["Runtime status", f"Timezone: {timezone}", ""]
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        connection.execute("BEGIN")
        for job_id, label in JOB_LABELS.items():
            label = localize_text(label)
            row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = ?", (_KEY_PREFIX + job_id,)
            ).fetchone()
            if row is None:
                lines.append(f"• {label}: {localize_text('No record yet')}")
                continue
            state = json.loads(row["value"])
            outcome = state["outcome"]
            if outcome == "running" and state.get("process_id") != _PROCESS_ID:
                outcome = "interrupted"
            lines.extend(
                (
                    f"• {label}: {localize_text(_OUTCOMES[outcome])}",
                    f"  Last started: {timestamp(state.get('started_at'))}",
                    f"  Last finished: {timestamp(state.get('finished_at'))}",
                    "  Last complete success: "
                    f"{timestamp(state.get('last_success_at'))}",
                    f"  {localize_text('No-data skips:')} {state.get('no_data', 0)}; "
                    f"{localize_text('Errors:')} {state.get('errors', 0)}; "
                    f"{localize_text('Delivery problems:')} "
                    f"{state.get('delivery_failures', 0)}",
                )
            )

        deliveries = dict(
            connection.execute(
                "SELECT status, COUNT(*) FROM notification_deliveries GROUP BY status"
            ).fetchall()
        )
        unassigned = connection.execute(
            """
            SELECT COUNT(*) FROM alert_events AS e
            WHERE e.notification_status IN ('pending', 'failed')
                AND NOT EXISTS (
                    SELECT 1 FROM notification_deliveries AS d WHERE d.event_id = e.id
                )
            """
        ).fetchone()[0]
        pending_dca = connection.execute(
            "SELECT COUNT(*) FROM scheduled_dca_occurrences WHERE status = 'pending'"
        ).fetchone()[0]
        pending_manual = connection.execute(
            "SELECT COUNT(*) FROM manual_add_estimates WHERE status = 'pending'"
        ).fetchone()[0]
        lines.extend(
            (
                "",
                f"Pending deliveries: {deliveries.get('pending', 0)}",
                f"In-flight deliveries: {deliveries.get('sending', 0)}",
                f"Failed deliveries: {deliveries.get('failed', 0)}",
                f"Unassigned reminder events: {unassigned}",
                f"Pending DCA estimates: {pending_dca}",
                f"Pending manual-add estimates: {pending_manual}",
                "Pending estimates may require NAV data, settings or position sync.",
            )
        )
        lines.extend(
            format_pending_work(connection, zone=zone, today=datetime.now(zone).date())
        )
        lines.extend(("", "Local market-data dates:"))
        histories = connection.execute(
            """
            SELECT symbol, asset_type, price_basis, MAX(date) AS date
            FROM market_daily_history GROUP BY symbol, asset_type, price_basis
            ORDER BY symbol, asset_type, price_basis
            LIMIT 6
            """
        ).fetchall()
        navs = connection.execute(
            """
            SELECT fund_symbol, MAX(nav_date) AS date
            FROM fund_nav_history GROUP BY fund_symbol ORDER BY fund_symbol LIMIT 6
            """
        ).fetchall()
        for row in histories[:5]:
            lines.append(
                f"• {row['symbol']} / {row['asset_type']} / "
                f"{row['price_basis']}: {row['date']}"
            )
        for row in navs[:5]:
            lines.append(
                f"• {localize_text('Fund NAV')} {row['fund_symbol']}: {row['date']}"
            )
        if not histories and not navs:
            lines.append("No cached market data")
        if len(histories) > 5 or len(navs) > 5:
            lines.append("Additional cached symbols omitted.")
    lines.extend(
        (
            "",
            "Local records only; no market requests were made.",
            "Cached dates do not prove all rules were evaluated successfully.",
        )
    )
    return localize_text("\n".join(lines))
