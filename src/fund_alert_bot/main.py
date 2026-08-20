"""Application entry point."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

from fund_alert_bot.commands import create_application, publish_bot_command_menu
from fund_alert_bot.config import load_settings
from fund_alert_bot.db import initialize_database, open_connection, prune_database
from fund_alert_bot.i18n import set_language
from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    CNMarketCalendar,
    install_akshare_proxy,
)
from fund_alert_bot.notifications.service import build_notification_service
from fund_alert_bot.scheduler import (
    create_scheduler,
    register_jobs,
    run_scheduled_fund_nav_process,
)

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    """Set up minimal process logging."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s [%(name)s] %(filename)s:%(lineno)d %(message)s"
        ),
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run() -> None:
    """Start the bot process."""
    configure_logging()
    settings = load_settings()
    set_language(settings.bot_language)
    proxy_active = install_akshare_proxy(
        enabled=settings.akshare_proxy_enabled,
        auth_token=settings.akshare_proxy_auth_token,
        retry=settings.akshare_proxy_retry,
    )

    startup_date = datetime.now(ZoneInfo(settings.timezone)).date()
    with open_connection(settings.sqlite_path) as connection:
        initialize_database(connection)
        pruned = prune_database(connection, today=startup_date)
    LOGGER.info(
        "Startup database retention prune completed date=%s deleted=%s",
        startup_date.isoformat(),
        {table: count for table, count in pruned.items() if count},
    )

    market_data_provider = AkshareMarketDataProvider(
        retries=settings.akshare_retries,
        retry_delay_seconds=settings.akshare_retry_delay_seconds,
        latest_lookback_days=settings.akshare_latest_lookback_days,
        history_cache_ttl_seconds=settings.akshare_history_cache_ttl_seconds,
        # The proxy patch owns retries for paid Eastmoney requests. Keeping
        # this provider budget at one avoids multiplying proxy attempts.
        eastmoney_retries=1 if proxy_active else None,
    )
    market_calendar = CNMarketCalendar()
    work_lock = Lock()
    scheduler = create_scheduler(timezone=settings.timezone)

    async def start_scheduler(application) -> None:
        await publish_bot_command_menu(application)
        if settings.akshare_proxy_enabled and not proxy_active:
            notification_service = build_notification_service(
                settings=settings.notifications,
                telegram_bot=application.bot,
                telegram_chat_ids=settings.telegram_allowed_user_ids,
            )
            await notification_service.send_alert(
                title="Paid proxy not enabled",
                body=(
                    "The paid proxy was not enabled because its balance is "
                    "insufficient or could not be verified.\n"
                    "Direct data sources will be used.\n"
                    "Recharge or fix the proxy token, then restart the bot."
                ),
            )
        register_jobs(
            scheduler,
            application=application,
            sqlite_path=settings.sqlite_path,
            allowed_user_ids=settings.telegram_allowed_user_ids,
            timezone=settings.timezone,
            check_time=settings.after_close_check_time,
            before_close_check_time=settings.before_close_check_time,
            dca_reminder_time=settings.dca_reminder_time,
            fund_nav_process_time=settings.fund_nav_process_time,
            market_data_provider=market_data_provider,
            market_calendar=market_calendar,
            notification_settings=settings.notifications,
            work_lock=work_lock,
        )
        scheduler.start()
        LOGGER.info("APScheduler started")

        async def startup_catchup() -> None:
            try:
                await run_scheduled_fund_nav_process(
                    application=application,
                    sqlite_path=settings.sqlite_path,
                    allowed_user_ids=settings.telegram_allowed_user_ids,
                    market_data_provider=market_data_provider,
                    market_calendar=market_calendar,
                    timezone=settings.timezone,
                    run_date=datetime.now(ZoneInfo(settings.timezone)).date(),
                    notification_settings=settings.notifications,
                    work_lock=work_lock,
                )
            except Exception:
                LOGGER.exception("Startup feeder-fund NAV catch-up failed")

        application.create_task(startup_catchup())

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
        market_calendar=market_calendar,
        notification_settings=settings.notifications,
        timezone=settings.timezone,
        work_lock=work_lock,
        post_init=start_scheduler,
        post_shutdown=stop_scheduler,
    )

    LOGGER.info(
        "fund-alert-bot starting with SQLite database at %s, "
        "%d allowed Telegram users, before-close realtime check %s %s, "
        "after-close check %s %s, DCA reminder check %s %s, "
        "fund NAV processing %s %s, language %s, paid Eastmoney proxy %s, "
        "Eastmoney retry budget %s, history cache TTL %ss",
        settings.sqlite_path,
        len(settings.telegram_allowed_user_ids),
        settings.before_close_check_time,
        settings.timezone,
        settings.after_close_check_time,
        settings.timezone,
        settings.dca_reminder_time,
        settings.timezone,
        settings.fund_nav_process_time,
        settings.timezone,
        settings.bot_language,
        proxy_active,
        1 if proxy_active else settings.akshare_retries,
        settings.akshare_history_cache_ttl_seconds,
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
