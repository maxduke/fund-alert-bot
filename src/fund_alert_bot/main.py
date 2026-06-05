"""Application entry point."""

from __future__ import annotations

import logging
import signal
from threading import Event

from fund_alert_bot.config import load_settings
from fund_alert_bot.db import initialize_database, open_connection
from fund_alert_bot.scheduler import create_scheduler, register_jobs

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    """Set up minimal process logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def run() -> None:
    """Start the bot process with no alert jobs registered yet."""
    configure_logging()
    settings = load_settings()

    with open_connection(settings.sqlite_path) as connection:
        initialize_database(connection)

    scheduler = create_scheduler(timezone=settings.timezone)
    register_jobs(scheduler)
    scheduler.start()

    stop_event = Event()

    def request_shutdown(_signum: int, _frame: object | None) -> None:
        LOGGER.info("Shutdown requested")
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_shutdown)

    LOGGER.info(
        "fund-alert-bot started with SQLite database at %s",
        settings.sqlite_path,
    )

    try:
        while not stop_event.wait(60):
            LOGGER.debug("fund-alert-bot heartbeat")
    finally:
        scheduler.shutdown(wait=False)
        LOGGER.info("fund-alert-bot stopped")


def main() -> None:
    """Console script wrapper."""
    run()


if __name__ == "__main__":
    main()
