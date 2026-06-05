"""Scheduler wiring."""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


def create_scheduler(*, timezone: str) -> Any:
    """Create an APScheduler instance without registering alert jobs yet."""
    from apscheduler.schedulers.background import BackgroundScheduler

    return BackgroundScheduler(timezone=timezone)


def register_jobs(scheduler: Any) -> None:
    """Register scheduled jobs.

    Alert jobs are intentionally not implemented in the initial skeleton.
    """
    LOGGER.info("No scheduler jobs registered yet")
