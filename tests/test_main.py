from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from fund_alert_bot import main, scheduler
from fund_alert_bot.config import NotificationSettings, Settings
from fund_alert_bot.db import (
    add_alert_event,
    add_enhanced_dca_rule,
    add_position_profit_rule,
    ensure_notification_delivery_targets,
    get_position_snapshot,
    get_scheduled_dca_occurrence,
    initialize_database,
    open_connection,
    upsert_position_snapshot,
)
from fund_alert_bot.market_data import FundNav


@pytest.mark.parametrize("dca_fails", [False, True])
def test_run_executes_startup_catchups_and_drains_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    dca_fails: bool,
) -> None:
    settings = _settings(tmp_path / "bot.sqlite3")
    scheduler = FakeScheduler()
    events: list[str] = []
    with open_connection(settings.sqlite_path) as connection:
        initialize_database(connection)
        event_id = add_alert_event(
            connection, rule_id=1, alert_key="old", title="Reminder", message="Old"
        )
        ensure_notification_delivery_targets(
            connection,
            event_ids=(event_id,),
            targets=(("telegram:123", "telegram"), ("telegram:456", "telegram")),
        )

    async def publish_menu(application) -> None:
        del application
        events.append("menu")

    async def nav_catchup(**kwargs) -> None:
        del kwargs
        events.append("nav")

    async def dca_catchup(**kwargs) -> int:
        del kwargs
        with open_connection(settings.sqlite_path) as connection:
            assert dict(
                connection.execute(
                    "SELECT target_key, status FROM notification_deliveries"
                ).fetchall()
            ) == {"telegram:123": "pending", "telegram:456": "cancelled"}
        events.append("dca")
        if dca_fails:
            raise RuntimeError("DCA recovery unavailable")
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

    assert events == ["timeout:30", "menu", "dca"] + ([] if dca_fails else ["nav"])
    assert scheduler.shutdown_waits == [True]


@pytest.mark.parametrize("amount, expected_alerts", [(2000, 0), (100, 1)])
def test_startup_settles_missed_dca_before_position_profit(
    monkeypatch: pytest.MonkeyPatch, tmp_path, amount: int, expected_alerts: int
) -> None:
    settings = _settings(tmp_path / "bot.sqlite3")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 1, 5, 10, tzinfo=tz)

    class Calendar:
        def confirmed_status(self, check_date):
            return check_date.weekday() < 5

    class Provider:
        def get_fund_nav(self, instrument, *, nav_date=None):
            assert nav_date == date(2024, 1, 4)
            return FundNav(instrument.symbol, nav_date, 2, "akshare_eastmoney")

    class Bot:
        async def send_message(self, **kwargs):
            pass

    async def publish_menu(application):
        pass

    def application_factory(**kwargs):
        application = FakeApplication(**kwargs)
        application.bot = Bot()
        return application

    with open_connection(settings.sqlite_path) as connection:
        initialize_database(connection)
        dca_id = add_enhanced_dca_rule(
            connection,
            fund_symbol="110026",
            name="Feeder fund",
            weekday="THU",
            amount=amount,
            fee_mode="rate",
            fee_value=0,
            holiday_policy="next",
            created_at="2024-01-01T00:00:00+00:00",
        )
        profit_id = add_position_profit_rule(
            connection, fund_symbol="110026", name="Feeder fund", thresholds=(0.2,)
        )
        upsert_position_snapshot(
            connection, fund_symbol="110026", units=100, average_unit_cost=1
        )
        connection.execute(
            "INSERT INTO app_metadata (key, value, updated_at) VALUES (?, ?, ?)",
            (scheduler.DCA_LAST_CHECKED_DATE_KEY, "2024-01-03", "2024-01-03"),
        )
        connection.commit()

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "install_akshare_proxy", lambda **kwargs: False)
    monkeypatch.setattr(main, "install_default_requests_timeout", lambda timeout: None)
    monkeypatch.setattr(main, "AkshareMarketDataProvider", lambda **kwargs: Provider())
    monkeypatch.setattr(main, "CNMarketCalendar", Calendar)
    monkeypatch.setattr(main, "create_scheduler", lambda **kwargs: FakeScheduler())
    monkeypatch.setattr(main, "register_jobs", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "publish_bot_command_menu", publish_menu)
    monkeypatch.setattr(main, "create_application", application_factory)

    # A second restart must not apply the recovered deduction or alert twice.
    for _ in range(2):
        main.run()
        with open_connection(settings.sqlite_path) as connection:
            occurrence = get_scheduled_dca_occurrence(connection, dca_id, "2024-01-04")
            assert occurrence["status"] == "applied"
            position = get_position_snapshot(connection, "110026")
            assert position["units"] == pytest.approx(100 + amount / 2)
            assert position["average_unit_cost"] == pytest.approx(
                (100 + amount) / (100 + amount / 2)
            )
            assert position["estimates_since_sync"] == 1
            evaluations = connection.execute(
                "SELECT nav_date FROM position_profit_evaluations WHERE rule_id = ?",
                (profit_id,),
            ).fetchall()
            assert [row["nav_date"] for row in evaluations] == ["2024-01-04"]
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM position_profit_thresholds WHERE rule_id = ?",
                    (profit_id,),
                ).fetchone()[0]
                == expected_alerts
            )


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
