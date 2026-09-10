from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fund_alert_bot import runtime_status, scheduler
from fund_alert_bot.checks import (
    DrawdownCheckResult,
    DrawdownPlanCheckResult,
    RuleNoDataSkip,
)
from fund_alert_bot.commands import build_command_handlers
from fund_alert_bot.db import (
    add_enhanced_dca_rule,
    add_rule,
    create_scheduled_dca_occurrence,
    ensure_notification_delivery_targets,
    initialize_database,
    open_connection,
    reserve_alert_event,
    upsert_fund_nav,
)
from fund_alert_bot.i18n import get_language, set_language
from fund_alert_bot.market_data import MarketCalendarUnavailableError
from fund_alert_bot.runtime_status import format_runtime_status


def _task_state(path, job_id=scheduler.MARKET_AFTER_CLOSE_JOB_ID):
    with open_connection(path) as connection:
        row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (f"job_status:{job_id}",),
        ).fetchone()
    return json.loads(row["value"])


def test_task_status_preserves_last_success_through_no_data_skip_and_failure(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "bot.sqlite3"
    kwargs = {
        "application": SimpleNamespace(bot=object()),
        "sqlite_path": path,
        "allowed_user_ids": {123},
        "market_data_provider": object(),
        "market_calendar": SimpleNamespace(is_trading_day=lambda _: True),
        "timezone": "Asia/Shanghai",
        "run_date": date(2026, 9, 9),
    }
    monkeypatch.setattr(
        scheduler,
        "evaluate_drawdown_rules",
        lambda *a, **k: DrawdownCheckResult(0, [], 0, [], [], []),
    )
    asyncio.run(scheduler.run_scheduled_market_check(**kwargs))
    successful = _task_state(path)
    assert successful["outcome"] == "ok"
    assert successful["last_success_at"] == successful["finished_at"]

    monkeypatch.setattr(
        scheduler,
        "evaluate_drawdown_rules",
        lambda *a, **k: DrawdownCheckResult(
            1, [], 0, [RuleNoDataSkip(1, "510300", "no current data")], [], []
        ),
    )
    asyncio.run(scheduler.run_scheduled_market_check(**kwargs))
    partial = _task_state(path)
    assert partial["outcome"] == "partial"
    assert partial["no_data"] == 1
    assert partial["last_success_at"] == successful["last_success_at"]

    kwargs["market_calendar"] = SimpleNamespace(is_trading_day=lambda _: False)
    asyncio.run(scheduler.run_scheduled_market_check(**kwargs))
    assert _task_state(path)["outcome"] == "skipped"

    def broken_calendar(_):
        raise RuntimeError("sensitive-exception-detail")

    kwargs["market_calendar"] = SimpleNamespace(is_trading_day=broken_calendar)
    with pytest.raises(RuntimeError):
        asyncio.run(scheduler.run_scheduled_market_check(**kwargs))
    failed = _task_state(path)
    assert failed["outcome"] == "failed"
    assert failed["errors"] == 1
    assert failed["last_success_at"] == successful["last_success_at"]
    assert "sensitive-exception-detail" not in json.dumps(failed)
    summary = format_runtime_status(path, timezone="Asia/Shanghai")
    assert "After-close check: Execution failed" in summary
    assert "Last complete success:" in summary


@pytest.mark.parametrize("rule_type", ["drawdown_from_high", "drawdown_plan"])
@pytest.mark.parametrize("current_day_confirmed", [False, True])
def test_before_close_calendar_gap_cannot_replace_last_success(
    tmp_path, monkeypatch, rule_type, current_day_confirmed
) -> None:
    path = tmp_path / "bot.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        for enabled in (True, False):
            add_rule(
                connection,
                type=rule_type,
                symbol="510300",
                name="Test drawdown",
                asset_type="cn_etf",
                params={},
                enabled=enabled,
            )
    drawdown = Mock(return_value=DrawdownCheckResult(1, [], 0, [], [], []))
    plan = Mock(return_value=DrawdownPlanCheckResult(1, [], [], []))
    monkeypatch.setattr(scheduler, "evaluate_drawdown_rules", drawdown)
    monkeypatch.setattr(scheduler, "evaluate_drawdown_plan_prealerts", plan)
    messages = []

    async def send_message(**kwargs):
        messages.append(kwargs["text"])

    run_date = date(2026, 9, 9)
    calendar = SimpleNamespace(
        is_trading_day=lambda _: True, confirmed_status=lambda _: True
    )
    kwargs = dict(
        application=SimpleNamespace(bot=SimpleNamespace(send_message=send_message)),
        sqlite_path=path,
        allowed_user_ids={123},
        market_data_provider=object(),
        market_calendar=calendar,
        timezone="Asia/Shanghai",
        run_date=run_date,
    )
    asyncio.run(scheduler.run_scheduled_before_close_check(**kwargs))
    successful = _task_state(path, scheduler.MARKET_BEFORE_CLOSE_JOB_ID)
    assert successful["outcome"] == "ok"
    drawdown.reset_mock()
    plan.reset_mock()

    def unavailable(check_date):
        if current_day_confirmed and check_date == run_date:
            return True
        raise MarketCalendarUnavailableError("Previous market date unavailable")

    calendar.confirmed_status = unavailable
    asyncio.run(scheduler.run_scheduled_before_close_check(**kwargs))
    partial = _task_state(path, scheduler.MARKET_BEFORE_CLOSE_JOB_ID)
    assert partial["outcome"] == "partial"
    assert partial["no_data"] == 1  # Disabled rules are not affected checks.
    assert partial["errors"] == partial["delivery_failures"] == 0
    assert partial["last_success_at"] == successful["last_success_at"]
    drawdown.assert_not_called()
    plan.assert_not_called()
    summary = format_runtime_status(path, timezone="Asia/Shanghai")
    assert "Before-close check: Incomplete (data or delivery problems)" in summary
    assert len(messages) == (1 if rule_type == "drawdown_plan" else 0)


def test_delivery_failure_is_not_a_complete_task_success(tmp_path) -> None:
    path = tmp_path / "bot.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        add_rule(
            connection,
            type="dca_reminder",
            symbol="test",
            name="Test",
            asset_type="dca",
            params={"weekday": "WED", "amount": 1000},
            created_at="2026-09-01T00:00:00+00:00",
        )

    class FailingBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("offline")

    asyncio.run(
        scheduler.run_due_dca_checks(
            application=SimpleNamespace(bot=FailingBot()),
            sqlite_path=path,
            allowed_user_ids={123},
            timezone="Asia/Shanghai",
            now=datetime(2026, 9, 9, 10),
        )
    )
    state = _task_state(path, scheduler.DCA_MORNING_JOB_ID)
    assert state["outcome"] == "partial"
    assert state["delivery_failures"] == 1
    assert "last_success_at" not in state
    assert "Failed deliveries: 1" in format_runtime_status(
        path, timezone="Asia/Shanghai"
    )


def test_task_status_reports_cancellation_and_crash_without_inventing_success(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "bot.sqlite3"
    started = asyncio.Event()
    release = asyncio.Event()

    @runtime_status.track_job(scheduler.FUND_NAV_PROCESS_JOB_ID)
    async def unfinished(**kwargs):
        started.set()
        await release.wait()

    async def scenario():
        task = asyncio.create_task(unfinished(sqlite_path=path))
        await started.wait()
        try:
            text = format_runtime_status(path, timezone="Asia/Shanghai")
            assert "Fund NAV processing: In progress" in text
            # A new process cannot claim that an old process's task is still running.
            monkeypatch.setattr(runtime_status, "_PROCESS_ID", "restarted-process")
            text = format_runtime_status(path, timezone="Asia/Shanghai")
            assert "Fund NAV processing: Interrupted; no completion recorded" in text
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    state = _task_state(path, scheduler.FUND_NAV_PROCESS_JOB_ID)
    assert state["outcome"] == "interrupted"
    assert "last_success_at" not in state


def test_newer_job_outcome_is_not_overwritten_by_an_older_run(tmp_path) -> None:
    path = tmp_path / "bot.sqlite3"

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        @runtime_status.track_job(scheduler.FUND_NAV_PROCESS_JOB_ID)
        async def work(*, sqlite_path, older):
            if older:
                started.set()
                await finish.wait()
            else:
                raise RuntimeError("newer run failed")

        older = asyncio.create_task(work(sqlite_path=path, older=True))
        await started.wait()
        try:
            with pytest.raises(RuntimeError):
                await work(sqlite_path=path, older=False)
        finally:
            finish.set()
            await older

    asyncio.run(scenario())
    assert _task_state(path, scheduler.FUND_NAV_PROCESS_JOB_ID)["outcome"] == "failed"


def test_status_command_is_local_authorized_localized_and_does_not_consume_state(
    tmp_path,
) -> None:
    path = tmp_path / "bot.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        rule_id = add_enhanced_dca_rule(
            connection,
            fund_symbol="110026",
            name="Test",
            weekday="WED",
            amount=1000,
            fee_mode="rate",
            fee_value=0,
            holiday_policy="next",
        )
        create_scheduled_dca_occurrence(
            connection,
            rule_id=rule_id,
            fund_symbol="110026",
            due_date="2026-09-09",
            gross_amount=1000,
            holiday_policy="next",
            effective_date="2026-09-09",
            skipped=False,
        )
        event_id = reserve_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="test",
            title="DCA reminder",
            message="test reminder",
        )
        ensure_notification_delivery_targets(
            connection, event_ids=[event_id], targets=[("telegram:123", "telegram")]
        )
        upsert_fund_nav(
            connection,
            fund_symbol="110026",
            nav_date=date(2026, 9, 8),
            unit_nav=1.2,
            source="test",
        )
        before = list(connection.iterdump())

    # Deliberately incapable objects ensure this path cannot request market data.
    handlers = build_command_handlers(
        {123}, sqlite_path=path, market_data_provider=object(), market_calendar=object()
    )
    handler = next(h for h in handlers if "status" in getattr(h, "commands", ()))
    replies = []

    async def reply_text(text):
        replies.append(text)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=999, type="private"),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    language = get_language()
    try:
        set_language("zh-CN")
        asyncio.run(handler.callback(update, SimpleNamespace()))
        assert replies == ["你无权使用此 Bot。"]
        update.effective_user.id = update.effective_chat.id = 123
        asyncio.run(handler.callback(update, SimpleNamespace()))
    finally:
        set_language(language)

    text = replies[-1]
    assert "运行状态" in text
    assert "收盘后检查: 尚无记录" in text
    assert "待投递目标数： 1" in text
    assert "待处理定投估算数： 1" in text
    assert "最早待处理的定投估算：" in text
    assert "待处理起始时间：" in text
    assert "110026: 2026-09-08" in text
    assert "本次没有请求行情" in text
    with open_connection(path) as connection:
        assert list(connection.iterdump()) == before


def test_status_bounds_cache_details_without_hiding_backlog_counts(tmp_path) -> None:
    path = tmp_path / "bot.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        for index in range(100):
            upsert_fund_nav(
                connection,
                fund_symbol=f"{index:06}",
                nav_date=date(2000, 1, 1),
                unit_nav=1.2,
                source="akshare_eastmoney",
            )
        before = list(connection.iterdump())
    language = get_language()
    try:
        for selected in ("en", "zh-CN"):
            set_language(selected)
            text = format_runtime_status(path, timezone="Asia/Shanghai")
            assert "000004: 2000-01-01" in text
            assert "000005: 2000-01-01" not in text
            assert len(text) < 2000
            if selected == "en":
                assert "Additional cached symbols omitted." in text
                assert "Pending DCA estimates: 0" in text
            else:
                assert "其余缓存标的已省略。" in text
                assert "待处理定投估算数： 0" in text
    finally:
        set_language(language)
    with open_connection(path) as connection:
        assert list(connection.iterdump()) == before
