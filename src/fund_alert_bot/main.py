"""Application entry point."""

from __future__ import annotations

import logging

from fund_alert_bot.commands import create_application
from fund_alert_bot.config import load_settings
from fund_alert_bot.db import initialize_database, open_connection
from fund_alert_bot.market_data import AkshareMarketDataProvider
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

    market_data_provider = AkshareMarketDataProvider()
    scheduler = create_scheduler(timezone=settings.timezone)

    async def start_scheduler(application) -> None:
        register_jobs(
            scheduler,
            application=application,
            sqlite_path=settings.sqlite_path,
            allowed_user_ids=settings.telegram_allowed_user_ids,
            timezone=settings.timezone,
            check_time=settings.after_close_check_time,
            market_data_provider=market_data_provider,
        )
        scheduler.start()
        LOGGER.info("APScheduler started")

    async def stop_scheduler(application) -> None:
        del application
        if getattr(scheduler, "running", False):
            scheduler.shutdown(wait=False)
            LOGGER.info("APScheduler stopped")

    application = create_application(
        token=settings.telegram_bot_token,
        allowed_user_ids=settings.telegram_allowed_user_ids,
        sqlite_path=settings.sqlite_path,
        market_data_provider=market_data_provider,
        post_init=start_scheduler,
        post_shutdown=stop_scheduler,
    )

    LOGGER.info(
        "fund-alert-bot starting with SQLite database at %s, "
        "%d allowed Telegram users, after-close check %s %s",
        settings.sqlite_path,
        len(settings.telegram_allowed_user_ids),
        settings.after_close_check_time,
        settings.timezone,
    )

    try:
        application.run_polling()
    finally:
        if getattr(scheduler, "running", False):
            scheduler.shutdown(wait=False)
        LOGGER.info("fund-alert-bot stopped")


def main() -> None:
    """Console script wrapper."""
    run()


if __name__ == "__main__":
    main()
