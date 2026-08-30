from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fund_alert_bot import main
from fund_alert_bot.config import NotificationSettings, Settings


def test_run_executes_startup_catchups_and_drains_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    settings = _settings(tmp_path / "bot.sqlite3")
    scheduler = FakeScheduler()
    events: list[str] = []

    async def publish_menu(application) -> None:
        del application
        events.append("menu")

    async def nav_catchup(**kwargs) -> None:
        del kwargs
        events.append("nav")

    async def dca_catchup(**kwargs) -> int:
        del kwargs
        events.append("dca")
        return 0

    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "install_akshare_proxy", lambda **kwargs: False)
    monkeypatch.setattr(
        main,
        "install_default_requests_timeout",
        lambda timeout: events.append(f"timeout:{timeout}"),
    )
    monkeypatch.setattr(main, "AkshareMarketDataProvider", lambda **kwargs: object())
    monkeypatch.setattr(main, "CNMarketCalendar", object)
    monkeypatch.setattr(main, "create_scheduler", lambda **kwargs: scheduler)
    monkeypatch.setattr(main, "register_jobs", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "publish_bot_command_menu", publish_menu)
    monkeypatch.setattr(main, "run_scheduled_fund_nav_process", nav_catchup)
    monkeypatch.setattr(main, "run_due_dca_checks", dca_catchup)
    monkeypatch.setattr(
        main,
        "create_application",
        lambda **kwargs: FakeApplication(**kwargs),
    )

    main.run()

    assert events == ["timeout:30", "menu", "nav", "dca"]
    assert scheduler.shutdown_waits == [True]


def test_run_rejects_configuration_without_notification_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        main,
        "load_settings",
        lambda: _settings(
            tmp_path / "bot.sqlite3",
            allowed_user_ids=frozenset(),
        ),
    )

    with pytest.raises(ValueError, match="At least one notification target"):
        main.run()


class FakeScheduler:
    def __init__(self) -> None:
        self.running = False
        self.shutdown_waits: list[bool] = []

    def start(self) -> None:
        self.running = True

    def shutdown(self, *, wait: bool) -> None:
        self.shutdown_waits.append(wait)
        self.running = False


class FakeApplication:
    def __init__(self, *, post_init, post_shutdown, **kwargs) -> None:
        del kwargs
        self.bot = SimpleNamespace()
        self._post_init = post_init
        self._post_shutdown = post_shutdown
        self._tasks: list[asyncio.Task] = []

    def create_task(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.append(task)
        return task

    def run_polling(self) -> None:
        async def lifecycle() -> None:
            await self._post_init(self)
            await asyncio.gather(*self._tasks)
            await self._post_shutdown(self)

        asyncio.run(lifecycle())


def _settings(sqlite_path, *, allowed_user_ids=frozenset({123})) -> Settings:
    return Settings(
        sqlite_path=sqlite_path,
        timezone="Asia/Shanghai",
        after_close_check_time="17:10",
        before_close_check_time="14:50",
        dca_reminder_time="09:30",
        fund_nav_process_time="08:30",
        bot_language="en",
        telegram_bot_token="token",
        telegram_allowed_user_ids=allowed_user_ids,
        akshare_retries=3,
        akshare_retry_delay_seconds=0,
        akshare_request_timeout_seconds=30,
        akshare_latest_lookback_days=30,
        akshare_history_cache_ttl_seconds=60,
        akshare_proxy_enabled=False,
        akshare_proxy_auth_token="",
        akshare_proxy_retry=1,
        notifications=NotificationSettings(),
    )
