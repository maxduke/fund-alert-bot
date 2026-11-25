"""Regression tests for PR 1 core correctness fixes."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from fund_alert_bot import checks, commands, scheduler
from fund_alert_bot.command_args import (
    parse_add_drawdown_plan_args,
    parse_mark_added_args,
)
from fund_alert_bot.db import (
    ALERT_NOTIFICATION_CANCELLED,
    add_alert_event,
    add_drawdown_plan_rule,
    add_position_profit_rule,
    claim_notification_deliveries,
    ensure_notification_delivery_targets,
    get_active_drawdown_cycle,
    get_drawdown_tier_reminder_states,
    initialize_database,
    open_connection,
    prune_database,
    record_manual_addition,
    refresh_alert_notification_status,
    upsert_fund_fee,
    upsert_market_history,
    upsert_position_snapshot,
)
from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    AssetType,
    CNMarketCalendar,
    FundNav,
    Instrument,
    MarketDataFetchError,
    PriceBasis,
)
from fund_alert_bot.market_data import akshare_provider as akshare_provider_module
from fund_alert_bot.market_data import calendar as calendar_module
from fund_alert_bot.plan_amounts import (
    amount_revision,
    apply_plan_amounts,
    preview_plan_amounts,
)
from fund_alert_bot.rules.dca import build_dca_reminder_alert
from fund_alert_bot.rules.drawdown_plan import DrawdownTier
from fund_alert_bot.rules.profit import build_position_profit_alert


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = 0
        self.edits: list[str] = []
        self.reply_markups: list[Any] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.answered += 1

    async def edit_message_text(self, text: str, reply_markup: Any = None) -> None:
        self.edits.append(text)
        self.reply_markups.append(reply_markup)


def _qfq_history(
    closes: list[float],
    *,
    start: date = date(2024, 1, 1),
    symbol: str = "510300",
) -> pd.DataFrame:
    dates = [start + timedelta(days=i) for i in range(len(closes))]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000.0] * len(closes),
            "amount": [10000.0] * len(closes),
            "source": ["akshare_eastmoney"] * len(closes),
        }
    )
    frame.attrs.update(
        {
            "symbol": symbol,
            "source": "akshare_eastmoney",
            "price_basis": "qfq",
            "frequency": "daily",
        }
    )
    return frame


def _callback_by_name(handlers: list[Any], name: str) -> Any:
    return next(
        handler
        for handler in handlers
        if getattr(handler.callback, "__name__", "") == name
    )


def test_drawdown_skip_callback_skips_correct_tier_when_earlier_tier_already_added(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        rule_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="CSI300",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "sma_window": 250,
                "sma_slope_window": 20,
                "rearm_margin": 0.02,
                "tiers": [
                    {"drawdown": 0.05, "amount": 500},
                    {"drawdown": 0.10, "amount": 1000},
                    {"drawdown": 0.15, "amount": 1500},
                ],
            },
        )
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        result = checks.evaluate_drawdown_plan_rule(
            connection,
            rule,
            _qfq_history([100.0, 80.0]),
            expected_date=date(2024, 1, 2),
        )
        assert result.notification is not None
        event_id = result.notification.event_id
        # Mark the first tier (-5%) as already added.
        record_manual_addition(
            connection,
            rule_id=rule_id,
            cycle_id=result.cycle_id,
            source_alert_event_id=event_id,
            fund_symbol="000001",
            tiers=(DrawdownTier(drawdown=0.05, amount=500, key="0.05"),),
            action_at=datetime(2024, 1, 2, 14, 0, tzinfo=UTC),
            create_estimate=False,
        )

    handlers = commands.build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        now_factory=lambda: datetime(2024, 1, 2, 6, 0, tzinfo=UTC),
    )
    callback = _callback_by_name(handlers, "drawdown_skip_callback")
    query = _FakeCallbackQuery(f"drawdown_skip:{rule_id}:{event_id}:choose")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=query,
    )

    asyncio.run(callback.callback(update, SimpleNamespace()))
    keyboard = query.reply_markups[-1].inline_keyboard
    # Row 0 = skip all, row 1 = -10%, row 2 = -15%
    tier_10_btn = keyboard[1][0]
    tier_15_btn = keyboard[2][0]
    assert "10%" in tier_10_btn.text
    assert "15%" in tier_15_btn.text

    # Tapping -15% must confirm and skip -15% (NOT -10%).
    query.data = tier_15_btn.callback_data
    asyncio.run(callback.callback(update, SimpleNamespace()))
    assert "-15%" in query.edits[-1]

    confirm_btn = query.reply_markups[-1].inline_keyboard[0][0]
    query.data = confirm_btn.callback_data
    asyncio.run(callback.callback(update, SimpleNamespace()))

    with open_connection(sqlite_path) as connection:
        states = get_drawdown_tier_reminder_states(connection, result.cycle_id)
    assert states["0.15"]["skipped_for_cycle"] == 1
    assert "0.1" not in states
    assert "0.05" not in states


def test_decimal_tier_percent_round_trips_and_tolerates_legacy_float_key(
    tmp_path: Path,
) -> None:
    parsed_plan = parse_add_drawdown_plan_args(
        ["510300", "000001", "Test", "19.9:1000"]
    )
    assert parsed_plan.config.tiers[0].key == "0.199"
    parsed_mark = parse_mark_added_args(["1", "19.9"])
    assert parsed_mark.tier_keys == ("0.199",)

    # Legacy plan saved with imprecise float 19.9 / 100 = 0.19899999999999998
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        rule_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="Legacy",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "sma_window": 250,
                "sma_slope_window": 20,
                "rearm_margin": 0.02,
                "tiers": [{"drawdown": 19.9 / 100, "amount": 1000}],
            },
        )
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        checks.evaluate_drawdown_plan_rule(
            connection,
            rule,
            _qfq_history([100.0, 75.0]),
            expected_date=date(2024, 1, 2),
        )
        selection = commands.load_manual_add_selection(
            connection,
            plan_id=rule_id,
            action_date=date(2024, 1, 2),
            tier_keys=parsed_mark.tier_keys,
        )
    assert len(selection.tiers) == 1
    assert selection.tiers[0].amount == 1000


def test_refresh_alert_notification_status_preserves_cancelled_with_zero_deliveries(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = add_alert_event(
            connection,
            rule_id=1,
            alert_key="cancelled-no-rows",
            title="Plan",
            message="Body",
        )
        connection.execute(
            "UPDATE alert_events SET notification_status = ? WHERE id = ?",
            (ALERT_NOTIFICATION_CANCELLED, event_id),
        )
        connection.commit()

        refresh_alert_notification_status(connection, event_ids=(event_id,))
        row = connection.execute(
            "SELECT notification_status FROM alert_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    assert row["notification_status"] == ALERT_NOTIFICATION_CANCELLED


def test_claim_notification_deliveries_empty_target_keys_returns_empty(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        event_id = add_alert_event(
            connection,
            rule_id=1,
            alert_key="empty-targets-test",
            title="Plan",
            message="Body",
        )
        ensure_notification_delivery_targets(
            connection,
            event_ids=(event_id,),
            targets=(("telegram:123", "telegram"),),
        )
        claimed = claim_notification_deliveries(
            connection,
            event_ids=(event_id,),
            target_keys=(),
        )
    assert claimed == []


def test_prune_ended_drawdown_cycles_preserves_amount_revision(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        rule_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="CSI300",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "sma_window": 250,
                "sma_slope_window": 20,
                "rearm_margin": 0.02,
                "tiers": [{"drawdown": 0.1, "amount": 1000}],
            },
        )
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        checks.evaluate_drawdown_plan_rule(
            connection,
            rule,
            _qfq_history([100.0, 95.0], start=date(2024, 1, 1)),
            expected_date=date(2024, 1, 2),
        )
        preview = preview_plan_amounts(
            connection,
            rule_id=rule_id,
            requested=(DrawdownTier(drawdown=0.1, amount=2000, key="0.1"),),
        )
        apply_plan_amounts(
            connection,
            preview,
            update_pending=False,
            confirmed_unpurchased=False,
            now=datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
        )
        rev_before = amount_revision(connection, rule_id)
        assert rev_before > 0

        # End cycle 1 by reaching a new peak above rearm margin (+3%).
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        checks.evaluate_drawdown_plan_rule(
            connection,
            rule,
            _qfq_history([100.0, 95.0, 103.0], start=date(2024, 1, 1)),
            expected_date=date(2024, 1, 3),
        )
        active = get_active_drawdown_cycle(connection, rule_id)
        assert active is not None and int(active["id"]) == 2

        prune_database(connection, today=date(2026, 6, 1))
        assert amount_revision(connection, rule_id) == rev_before


def test_read_drawdown_plan_statuses_refetches_stale_cache_below_minimum_date(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    calls: list[tuple[date, date]] = []

    class RefreshingProvider:
        def get_history(
            self,
            instrument: Instrument,
            start_date: date,
            end_date: date,
            *,
            price_basis: PriceBasis = PriceBasis.UNADJUSTED,
        ) -> pd.DataFrame:
            del instrument, price_basis
            calls.append((start_date, end_date))
            return _qfq_history(
                [100.0] * 600,
                start=date(2022, 6, 1),
            )

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="CSI300",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 30,
                "sma_window": 10,
                "sma_slope_window": 5,
                "rearm_margin": 0.02,
                "tiers": [{"drawdown": 0.1, "amount": 1000}],
            },
        )
        # Seed stale cache covering start_date..2023-12-01 (1 month stale).
        stale_dates = [date(2023, 10, 1) + timedelta(days=i) for i in range(62)]
        upsert_market_history(
            connection,
            symbol="510300",
            asset_type="cn_etf",
            price_basis="qfq",
            rows=[
                {
                    "date": d.isoformat(),
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "source": "akshare_eastmoney",
                }
                for d in stale_dates
            ],
        )
        result = checks.read_drawdown_plan_statuses(
            connection,
            RefreshingProvider(),
            end_date=date(2024, 1, 10),
            minimum_fund_nav_date=date(2024, 1, 9),
        )
    assert len(calls) == 1
    assert len(result.statuses) == 1
    assert result.statuses[0].evaluation.latest_date >= date(2024, 1, 9)


def test_prealert_skips_when_after_close_already_evaluated_market_date(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"

    class UnusedProvider:
        def get_history(self, *args: object, **kwargs: object) -> pd.DataFrame:
            raise AssertionError("Should not fetch history after same-day close")

        def get_etf_realtime_quote(self, *args: object, **kwargs: object) -> Any:
            raise AssertionError("Should not fetch realtime quote after same-day close")

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        rule_id = add_drawdown_plan_rule(
            connection,
            reference_symbol="510300",
            investment_fund_symbol="000001",
            name="CSI300",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "sma_window": 250,
                "sma_slope_window": 20,
                "rearm_margin": 0.02,
                "tiers": [{"drawdown": 0.1, "amount": 1000}],
            },
        )
        rule = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        checks.evaluate_drawdown_plan_rule(
            connection,
            rule,
            _qfq_history([100.0, 99.0], start=date(2024, 1, 1)),
            expected_date=date(2024, 1, 2),
        )
        prealert_result = checks.evaluate_drawdown_plan_prealerts(
            connection,
            UnusedProvider(),
            market_date=date(2024, 1, 2),
            confirmed_date=date(2024, 1, 1),
        )
    assert prealert_result.errors == []
    assert prealert_result.no_data_skips == []
    assert prealert_result.notifications == []


def test_build_dca_notification_summary_preserves_holiday_skip_and_handles_short() -> (
    None
):
    alert = build_dca_reminder_alert(
        {
            "id": 7,
            "symbol": "000001",
            "name": "Fund A",
            "asset_type": "cn_open_fund",
            "params": {
                "weekday": "MON",
                "amount": 500,
                "holiday_policy": "skip",
            },
        },
        date(2024, 1, 1),
        lambda _key: False,
        occurrence_status="skipped",
    )
    assert alert is not None
    summary = checks.build_dca_notification_summary(
        message=str(alert["message"]),
        due_date="2024-01-01",
        amount=500.0,
        skipped=True,
        current_status="skipped",
    )
    assert summary.rebuilt_text is not None
    assert "No action is required for this configured holiday skip." in (
        summary.rebuilt_text
    )

    short_summary = checks.build_dca_notification_summary(
        message="short message",
        due_date="2024-01-01",
        amount=100.0,
        skipped=False,
        current_status="applied",
    )
    assert short_summary.due_date == "2024-01-01"


def test_position_profit_deduplicates_crossed_and_checks_membership(
    tmp_path: Path,
) -> None:
    built = build_position_profit_alert(
        {
            "id": 1,
            "symbol": "000001",
            "name": "Fund A",
            "params": {"cost": "auto", "thresholds": [0.1, 0.1, 0.2]},
        },
        FundNav("000001", date(2024, 1, 2), 1.3, "akshare_eastmoney"),
        {"units": 100.0, "average_unit_cost": 1.0, "is_estimated": False},
        position_cycle_id=1,
        recorded_threshold_keys=set(),
    )
    assert built is not None
    _alert, crossed = built
    assert [key for key, _val in crossed] == ["0.1", "0.2"]

    # Recorded key "0.05" != configured ["0.2"]: count matches (1 >= 1), but
    # membership check must NOT skip evaluating "0.2".
    sqlite_path = tmp_path / "bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        rule_id = add_position_profit_rule(
            connection,
            fund_symbol="000001",
            name="Fund A",
            thresholds=(0.2,),
        )
        upsert_fund_fee(
            connection, fund_symbol="000001", fee_mode="rate", fee_value=0.0
        )
        upsert_position_snapshot(
            connection, fund_symbol="000001", units=100.0, average_unit_cost=1.0
        )
        connection.execute(
            """
            INSERT INTO position_profit_thresholds
                (rule_id, position_cycle_id, threshold_key, threshold, created_at)
            VALUES (?, 1, '0.05', 0.05, '2024-01-01T00:00:00+00:00')
            """,
            (rule_id,),
        )
        connection.commit()

        class FakeCal:
            def confirmed_status(self, d: date) -> bool:
                return True

        class FakeProv:
            def get_fund_nav(
                self, instrument: Instrument, *, nav_date: date | None = None
            ) -> FundNav:
                return FundNav(
                    instrument.symbol,
                    nav_date or date(2024, 1, 2),
                    1.3,
                    "akshare_eastmoney",
                )

        result = checks.evaluate_position_profit_rules(
            connection,
            FakeProv(),
            FakeCal(),
            processing_date=date(2024, 1, 3),
        )
    assert len(result.notifications) == 1
    assert "20.0%" in result.notifications[0].text


def test_akshare_provider_cache_prefix_and_nav_retry_fixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(akshare_provider_module.time, "monotonic", lambda: now[0])

    class FakeAk:
        def __init__(self) -> None:
            self.etf_symbols: list[str] = []
            self.stock_symbols: list[str] = []
            self.nav_calls: list[str] = []
            self.include_today = False
            self.fail_nav = False

        def fund_etf_hist_em(self, **kwargs: Any) -> pd.DataFrame:
            self.etf_symbols.append(str(kwargs["symbol"]))
            dates = ["2024-01-01", "2024-01-02"]
            if self.include_today:
                dates.append("2024-01-03")
            return pd.DataFrame(
                {
                    "日期": dates,
                    "开盘": [1.0] * len(dates),
                    "最高": [1.0] * len(dates),
                    "最低": [1.0] * len(dates),
                    "收盘": [1.0] * len(dates),
                    "成交量": [100] * len(dates),
                    "成交额": [1000] * len(dates),
                }
            )

        def stock_zh_a_hist(self, **kwargs: Any) -> pd.DataFrame:
            self.stock_symbols.append(str(kwargs["symbol"]))
            return self.fund_etf_hist_em(**kwargs)

        def fund_open_fund_info_em(self, **kwargs: Any) -> pd.DataFrame:
            self.nav_calls.append(str(kwargs["symbol"]))
            if self.fail_nav:
                raise RuntimeError("transient nav error")
            return pd.DataFrame({"净值日期": ["2024-01-02"], "单位净值": ["1.23"]})

    fake_ak = FakeAk()
    provider = AkshareMarketDataProvider(
        ak_module=fake_ak,
        retries=3,
        eastmoney_retries=1,
        retry_delay_seconds=0,
        history_cache_ttl_seconds=300,
        fund_nav_cache_ttl_seconds=300,
        eastmoney_failure_ttl_seconds=30,
        today_factory=lambda: date(2024, 1, 3),
    )

    # 1) Prefixed symbols are stripped for Eastmoney ETF & stock history.
    # 2) Incomplete request for today (only through 2024-01-02) is NOT cached.
    first = provider.get_history(
        Instrument("sh510300", "ETF", AssetType.CN_ETF),
        date(2024, 1, 1),
        date(2024, 1, 3),
        price_basis=PriceBasis.QFQ,
    )
    assert fake_ak.etf_symbols == ["510300"]
    assert first["date"].iloc[-1].date() == date(2024, 1, 2)

    fake_ak.include_today = True
    second = provider.get_history(
        Instrument("sh510300", "ETF", AssetType.CN_ETF),
        date(2024, 1, 1),
        date(2024, 1, 3),
        price_basis=PriceBasis.QFQ,
    )
    assert second["date"].iloc[-1].date() == date(2024, 1, 3)

    provider.get_history(
        Instrument("sh600519", "Stock", AssetType.CN_STOCK),
        date(2024, 1, 1),
        date(2024, 1, 2),
    )
    assert fake_ak.stock_symbols == ["600519"]

    # 3) fund_open_fund_info_em uses full self._retries (3), not eastmoney_retries (1),
    #    and negative cache expires after eastmoney_failure_ttl_seconds (30s), not 300s.
    fake_ak.fail_nav = True
    with pytest.raises(MarketDataFetchError):
        provider.get_fund_nav(Instrument("000001", "Fund A", AssetType.CN_OPEN_FUND))
    assert fake_ak.nav_calls == ["000001", "000001", "000001"]

    # Suppressed Fund B must NOT get negative-cached for 300s.
    with pytest.raises(MarketDataFetchError, match="retry suppressed"):
        provider.get_fund_nav(Instrument("000002", "Fund B", AssetType.CN_OPEN_FUND))

    now[0] = 31.0
    fake_ak.fail_nav = False
    nav_b = provider.get_fund_nav(
        Instrument("000002", "Fund B", AssetType.CN_OPEN_FUND)
    )
    nav_a = provider.get_fund_nav(
        Instrument("000001", "Fund A", AssetType.CN_OPEN_FUND)
    )
    assert nav_b.value == pytest.approx(1.23)
    assert nav_a.value == pytest.approx(1.23)


def test_calendar_recovers_after_failure_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(calendar_module.time, "monotonic", lambda: now[0])

    class FlakyCalendar:
        def __init__(self) -> None:
            self.calls = 0
            self.fail = True

        def tool_trade_date_hist_sina(self) -> pd.DataFrame:
            self.calls += 1
            if self.fail:
                raise RuntimeError("temporary outage")
            return pd.DataFrame({"trade_date": ["2024-05-01", "2024-05-06"]})

    fake = FlakyCalendar()
    cal = CNMarketCalendar(
        ak_module=fake,
        today_factory=lambda: date(2024, 5, 6),
        failure_retry_cooldown_seconds=60.0,
    )
    assert cal.is_trading_day(date(2024, 5, 6)) is True
    assert fake.calls == 1
    # Within cooldown, does not hammer upstream.
    now[0] = 30.0
    assert cal.is_trading_day(date(2024, 5, 6)) is True
    assert fake.calls == 1
    # After cooldown expires, retries and recovers same day.
    now[0] = 61.0
    fake.fail = False
    assert cal.confirmed_status(date(2024, 5, 2)) is False
    assert fake.calls == 2


def test_write_dca_check_date_is_monotonic(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bot.sqlite3"
    scheduler._write_dca_check_date(sqlite_path, date(2024, 1, 10))
    scheduler._write_dca_check_date(sqlite_path, date(2024, 1, 8))
    with open_connection(sqlite_path) as connection:
        row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (scheduler.DCA_LAST_CHECKED_DATE_KEY,),
        ).fetchone()
    assert row["value"] == "2024-01-10"
