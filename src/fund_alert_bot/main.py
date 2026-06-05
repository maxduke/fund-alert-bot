"""Application entry point."""

from __future__ import annotations

import logging

from fund_alert_bot.commands import create_application
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
    """Start the bot process."""
    configure_logging()
    settings = load_settings()

    with open_connection(settings.sqlite_path) as connection:
        initialize_database(connection)

    application = create_application(
        token=settings.telegram_bot_token,
        allowed_user_ids=settings.telegram_allowed_user_ids,
        sqlite_path=settings.sqlite_path,
    )

    scheduler = create_scheduler(timezone=settings.timezone)
    register_jobs(scheduler)
    scheduler.start()

    LOGGER.info(
        "fund-alert-bot started with SQLite database at %s "
        "and %d allowed Telegram users",
        settings.sqlite_path,
        len(settings.telegram_allowed_user_ids),
    )

    try:
        application.run_polling()
    finally:
        scheduler.shutdown(wait=False)
        LOGGER.info("fund-alert-bot stopped")


def main() -> None:
    """Console script wrapper."""
    run()


if __name__ == "__main__":
    main()
