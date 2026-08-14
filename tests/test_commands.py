from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from fund_alert_bot.checks import (
    _drawdown_plan_action_rows,
    evaluate_drawdown_plan_rule,
    evaluate_position_profit_rules,
)
from fund_alert_bot.commands import (
    BOT_COMMAND_MENU,
    DCA_RULE_TYPE,
    DRAW_DOWN_RULE_TYPE,
    PROFIT_RULE_TYPE,
    TEST_NOTIFICATION_MESSAGE,
    CommandParseError,
    ManualAddSelection,
    _reply_text,
    build_command_handlers,
    build_drawdown_plan_preview,
    dca_params,
    drawdown_params,
    evaluate_drawdown_rules,
    evaluate_profit_rules,
    format_check_summary,
    format_manual_add_confirmation,
    format_rules_list,
    load_manual_add_selection,
    parse_add_dca_args,
    parse_add_drawdown_args,
    parse_add_drawdown_plan_args,
    parse_add_profit_args,
    parse_mark_added_args,
    parse_set_fund_cutoff_args,
    parse_set_fund_fee_args,
    parse_sync_position_args,
    parse_thresholds,
    profit_params,
    publish_bot_command_menu,
)
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import (
    add_rule,
    connect,
    get_active_drawdown_cycle,
    get_active_position_cycle,
    get_fund_settings,
    get_position_snapshot,
    init_db,
    list_pending_position_items,
    list_rules,
    open_connection,
    record_manual_addition,
    upsert_fund_cutoff,
    upsert_fund_fee,
    upsert_market_history,
    upsert_position_snapshot,
)
from fund_alert_bot.i18n import get_language, set_language
from fund_alert_bot.market_data import (
    AssetType,
    FundNav,
    Instrument,
    MarketCalendarUnavailableError,
    PriceBasis,
)
from fund_alert_bot.rules.dca import weekday_for_date
from fund_alert_bot.rules.drawdown_plan import DrawdownTier

EXPECTED_DRAWDOWN_10_MESSAGE = "\n".join(
    (
        "📉 Drawdown reminder",
        "",
        "• Symbol: 399006",
        "• Name: 创业板指",
        "• Asset type: cn_index",
        "• Lookback: 365 days",
        "• Drawdown: 10.0%",
        "• Triggered threshold: 10.0%",
        "• Peak: 100 on 2024-01-01",
        "• Latest: 90 on 2024-01-02",
        "",
        "Reminder: this is not automatic trading and no orders will be placed.",
    )
)

EXPECTED_DCA_MESSAGE = "\n".join(
    (
        "💰 DCA reminder",
        "",
        "• Asset: 创业板",
        "• Date: 2024-01-04",
        "• Planned amount: 1000 RMB",
        "",
        "Reminder: this is a discipline reminder; no automatic trade occurs.",
    )
)

EXPECTED_PROFIT_MESSAGE = "\n".join(
    (
        "💰 Price-Gain reminder",
        "",
        "• Symbol: 159915",
        "• Name: ChiNext ETF",
        "• Asset type: cn_etf",
        "• Cost: 1.85",
        "• Latest price: 2.4",
        "• Data date: 2024-01-02",
        "• Source: test",
        "• Profit rate: 29.7%",
        "• Triggered threshold: 25.0%",
        "",
        "This is a price-gain reminder only.",
        "No trade has been placed.",
    )
)


def test_publish_bot_command_menu_matches_handlers_and_localizes(caplog) -> None:
    command_names = {
        command
        for handler in build_command_handlers({123})
        for command in getattr(handler, "commands", ())
    }
    assert {command for command, _description in BOT_COMMAND_MENU} == command_names

    original_language = get_language()
    bot = FakeCommandMenuBot()
    try:
        set_language("zh-CN")
        asyncio.run(publish_bot_command_menu(SimpleNamespace(bot=bot)))
    finally:
        set_language(original_language)

    assert bot.commands[0] == ("start", "启动 Bot")

    bot.fail = True
    asyncio.run(publish_bot_command_menu(SimpleNamespace(bot=bot)))
    assert "command menu registration failed" in caplog.text


def test_parse_valid_drawdown_command() -> None:
    command = parse_add_drawdown_args(
        ["cn_index", "399006", "创业板指", "365", "10,15,20"]
    )

    assert command.asset_type is AssetType.CN_INDEX
    assert command.symbol == "399006"
    assert command.name == "创业板指"
    assert command.lookback_days == 365
    assert drawdown_params(command) == {
        "lookback_days": 365,
        "thresholds": [0.10, 0.15, 0.20],
        "price_field": "close",
    }


def test_reply_text_splits_long_messages_and_keeps_buttons_on_last_page() -> None:
    message = FakeMessage()
    markup = object()
    text = "x" * 9000
    update = SimpleNamespace(effective_message=message)

    asyncio.run(_reply_text(update, text, reply_markup=markup))

    assert "".join(message.replies) == text
    assert all(len(chunk) <= 4096 for chunk in message.replies)
    assert [page_markup for _chunk, page_markup in message.reply_calls] == [
        None,
        None,
        markup,
    ]

    boundary_message = FakeMessage()
    asyncio.run(
        _reply_text(
            SimpleNamespace(effective_message=boundary_message),
            "x" * 4096 + "\n",
        )
    )
    assert boundary_message.reply_calls == [("x" * 4096, None)]


def test_parse_valid_profit_command() -> None:
    command = parse_add_profit_args(
        ["cn_open_fund", "110026", "Example Fund", "1.234", "25,40"]
    )

    assert command.asset_type is AssetType.CN_OPEN_FUND
    assert command.symbol == "110026"
    assert command.name == "Example Fund"
    assert command.cost == 1.234
    assert command.thresholds == [0.25, 0.40]
    assert profit_params(command) == {
        "cost": 1.234,
        "thresholds": [0.25, 0.40],
    }


def test_parse_valid_dca_command_with_chinese_weekday() -> None:
    command = parse_add_dca_args(["创业板", "周四", "1000"])

    assert command.name == "创业板"
    assert command.weekday == "THU"
    assert command.amount == 1000
    assert dca_params(command) == {"weekday": "THU", "amount": 1000}


def test_parse_valid_dca_command_with_english_weekday() -> None:
    command = parse_add_dca_args(["创业板", "Thursday", "1000"])

    assert command.name == "创业板"
    assert command.weekday == "THU"
    assert command.amount == 1000


def test_parse_enhanced_dca_command_defaults_holiday_next() -> None:
    command = parse_add_dca_args(
        ["110026", '"A500 feeder"', "周四", "2000", "rate:0.12%"]
    )

    assert command.fund_symbol == "110026"
    assert command.name == "A500 feeder"
    assert command.weekday == "THU"
    assert command.amount == 2000
    assert command.fee_mode == "rate"
    assert command.fee_value == pytest.approx(0.0012)
    assert command.holiday_policy == "next"
    assert dca_params(command) == {
        "weekday": "THU",
        "amount": 2000,
        "holiday_policy": "next",
    }


def test_parse_enhanced_dca_rejects_fixed_fee_consuming_amount() -> None:
    with pytest.raises(CommandParseError, match="lower than"):
        parse_add_dca_args(
            ["110026", "A500", "周四", "100", "fixed:100", "holiday:skip"]
        )


def test_fractional_drawdown_tiers_keep_precision_in_actions_and_confirmation() -> None:
    tiers = (
        DrawdownTier(0.151, 100.005, "0.151"),
        DrawdownTier(0.154, 100.006, "0.154"),
    )

    rows = _drawdown_plan_action_rows(1, 2, tiers)
    confirmation = format_manual_add_confirmation(
        ManualAddSelection(
            plan_id=1,
            event_id=2,
            cycle_id=3,
            fund_symbol="000001",
            name="A500",
            tiers=tiers,
            readiness="READY",
            missing_setup=(),
            cutoff="15:00",
            market_date=date(2024, 1, 2),
        )
    )

    assert [rows[1][0][0], rows[2][0][0]] == [
        "仅记录 -15.1% ¥100.005",
        "仅记录 -15.4% ¥100.006",
    ]
    assert "• -15.1% → ¥100.005" in confirmation
    assert "• -15.4% → ¥100.006" in confirmation
    assert "Configured gross total: ¥200.011" in confirmation


def test_drawdown_tier_callbacks_use_bounded_indexes() -> None:
    tiny_key = "0." + "0" * 39 + "1"
    tiers = (
        DrawdownTier(1e-40, 100, tiny_key),
        DrawdownTier(0.15, 200, "0.15"),
    )

    rows = _drawdown_plan_action_rows(2**63 - 1, 2**63 - 1, tiers)
    callback_data = [rows[1][0][1], rows[2][0][1]]

    assert callback_data[0].endswith(":tier:0")
    assert callback_data[1].endswith(":tier:1")
    assert all(len(value.encode()) <= 64 for value in callback_data)


def test_parse_drawdown_plan_with_quoted_name_and_optional_lookback() -> None:
    command = parse_add_drawdown_plan_args(
        [
            "510300",
            "000001",
            '"A500 Core"',
            "15:5000,20:10000,25:15000",
            "lookback:730",
        ]
    )

    assert command.reference_symbol == "510300"
    assert command.investment_fund_symbol == "000001"
    assert command.name == "A500 Core"
    assert command.config.lookback_days == 730
    assert [tier.amount for tier in command.config.tiers] == [5000, 10000, 15000]
    assert command.params["sma_window"] == 250
    assert command.params["sma_slope_window"] == 20


def test_parse_drawdown_plan_rejects_oversized_rendered_notifications() -> None:
    tiers = ",".join(
        f"{10 + index * 1e-12:.12f}:1234567890123.45" for index in range(50)
    )

    with pytest.raises(CommandParseError, match="4096-character"):
        parse_add_drawdown_plan_args(["510300", "000001", "N" * 600, tiers])


@pytest.mark.parametrize(
    "args,message",
    [
        (["510300", "510300", "A500", "15:5000"], "must differ"),
        (["５１０３００", "000001", "A500", "15:5000"], "exactly 6 digits"),
        (["510300", "000001", "A500", "20:1,15:2"], "strictly ascending"),
        (["510300", "000001", "A500", "15:0"], "positive finite"),
        (
            ["510300", "000001", "A500", "15:5000", "sma:200"],
            "only trailing lookback",
        ),
    ],
)
def test_reject_invalid_drawdown_plan_command(
    args: list[str],
    message: str,
) -> None:
    with pytest.raises(CommandParseError, match=message):
        parse_add_drawdown_plan_args(args)


def test_parse_fund_settings_and_position_commands() -> None:
    rate = parse_set_fund_fee_args(["110026", "rate:0.15%"])
    fixed = parse_set_fund_fee_args(["110026", "fixed:1.5"])
    cutoff = parse_set_fund_cutoff_args(["110026", "15:00"])
    position = parse_sync_position_args(["110026", "1234.5", "1.234"])
    closed = parse_sync_position_args(["110026", "0", "0"])

    assert (rate.fee_mode, rate.fee_value) == ("rate", 0.0015)
    assert (fixed.fee_mode, fixed.fee_value) == ("fixed", 1.5)
    assert cutoff.subscription_cutoff == "15:00"
    assert (position.units, position.average_unit_cost) == (1234.5, 1.234)
    assert (closed.units, closed.average_unit_cost) == (0, 0)

    marked = parse_mark_added_args(["12", "15,20"])
    assert (marked.plan_id, marked.tier_keys) == (12, ("0.15", "0.2"))


@pytest.mark.parametrize(
    "parser,args,message",
    [
        (parse_set_fund_fee_args, ["110026", "0.15%"], "fee must use"),
        (parse_set_fund_fee_args, ["110026", "rate:nan%"], "finite"),
        (parse_set_fund_cutoff_args, ["110026", "24:00"], "24-hour"),
        (parse_sync_position_args, ["110026", "10", "0"], "exact 0 0"),
        (parse_sync_position_args, ["110026", "nan", "1"], "finite"),
        (parse_sync_position_args, ["ABC", "0", "0"], "exactly 6 digits"),
        (parse_sync_position_args, ["１１００２６", "0", "0"], "exactly 6 digits"),
    ],
)
def test_reject_invalid_fund_settings_and_position_commands(
    parser: object,
    args: list[str],
    message: str,
) -> None:
    with pytest.raises(CommandParseError, match=message):
        parser(args)


def test_reject_invalid_asset_type() -> None:
    with pytest.raises(CommandParseError, match="Invalid asset_type"):
        parse_add_drawdown_args(["crypto", "BTC", "Bitcoin", "365", "10"])

    with pytest.raises(CommandParseError, match="Invalid asset_type"):
        parse_add_profit_args(["crypto", "BTC", "Bitcoin", "100", "25"])


def test_parse_thresholds_correctly() -> None:
    assert parse_thresholds("10,15,20") == [0.10, 0.15, 0.20]


def test_check_prevents_duplicate_alert_notifications() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指",
            asset_type=AssetType.CN_INDEX.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.10],
                "price_field": "close",
            },
        )
        provider = FakeProvider(_history(["2024-01-01", "2024-01-02"], [100.0, 90.0]))

        first_result = evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 2),
        )
        second_result = evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 2),
        )
        event_count = connection.execute(
            "SELECT COUNT(*) FROM alert_events"
        ).fetchone()[0]

    finally:
        connection.close()

    assert len(first_result.notifications) == 1
    assert len(second_result.notifications) == 0
    assert event_count == 1
    # The fixture is intentionally shorter than the requested 365-day range;
    # the persistent cache must re-fetch rather than treat it as complete.
    assert [call[0].asset_type for call in provider.calls] == [
        AssetType.CN_INDEX,
        AssetType.CN_INDEX,
    ]


def test_before_close_latest_quote_is_not_persisted_as_a_close() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指",
            asset_type=AssetType.CN_INDEX.value,
            params={"lookback_days": 365, "thresholds": [0.10]},
        )
        provider = FakeProvider(
            _history(["2024-01-01", "2024-01-02"], [100.0, 90.0]),
            latest={"date": "2024-01-02", "close": 90.0, "source": "test"},
        )
        evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 2),
            include_latest=True,
            confirmed_end_date=date(2024, 1, 1),
        )
        dates = [
            row["date"]
            for row in connection.execute(
                "SELECT date FROM market_daily_history ORDER BY date"
            ).fetchall()
        ]
    finally:
        connection.close()

    assert dates == ["2024-01-01"]


def test_before_close_refreshes_stale_confirmed_history_before_appending_quote() -> (
    None
):
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指",
            asset_type=AssetType.CN_INDEX.value,
            params={"lookback_days": 365, "thresholds": [0.10]},
        )
        stale = _history(["2024-01-01"], [100.0])
        upsert_market_history(
            connection,
            symbol="399006",
            asset_type=AssetType.CN_INDEX.value,
            price_basis=PriceBasis.UNADJUSTED.value,
            rows=stale.to_dict("records"),
        )
        provider = FakeProvider(
            _history(["2024-01-01", "2024-01-02"], [100.0, 110.0]),
            latest={"date": "2024-01-03", "close": 100.0, "source": "test"},
        )

        result = evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 3),
            include_latest=True,
            confirmed_end_date=date(2024, 1, 2),
        )
    finally:
        connection.close()

    assert len(provider.calls) == 1
    assert result.statuses[0].peak_price == pytest.approx(110.0)
    assert result.statuses[0].drawdown == pytest.approx(1 - 100 / 110)


def test_manual_check_summary_shows_current_drawdown_percent() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指",
            asset_type=AssetType.CN_INDEX.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.20],
                "price_field": "close",
            },
        )
        provider = FakeProvider(_history(["2024-01-01", "2024-01-02"], [100, 90]))

        result = evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 2),
        )
        response = format_check_summary(result)
    finally:
        connection.close()

    assert "📉 Current drawdowns" in response
    assert "Rule 1 399006 · 创业板指: 10.0% from high 100 on 2024-01-01" in response


def test_drawdown_check_reuses_history_for_same_code_ranges() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指",
            asset_type=AssetType.CN_INDEX.value,
            params={
                "lookback_days": 30,
                "thresholds": [0.10],
                "price_field": "close",
            },
        )
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指-alt",
            asset_type=AssetType.CN_INDEX.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.15],
                "price_field": "close",
            },
        )
        provider = FakeProvider(
            _history(["2023-01-02", "2024-01-02"], [100.0, 85.0]),
            latest={"date": "2024-01-02", "close": 84.0, "source": "test"},
        )

        evaluate_drawdown_rules(
            connection,
            provider,
            today=date(2024, 1, 2),
            include_latest=True,
        )
    finally:
        connection.close()

    assert len(provider.calls) == 1
    assert provider.calls[0][1] == date(2022, 12, 19)
    assert provider.calls[0][2] == date(2024, 1, 2)
    assert len(provider.latest_calls) == 1


def test_check_retries_alert_after_delivery_failure(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="399006",
            name="创业板指",
            asset_type=AssetType.CN_INDEX.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.10],
                "price_field": "close",
            },
        )

    provider = FakeProvider(_history(["2024-01-01", "2024-01-02"], [100.0, 90.0]))
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
        market_calendar=FakeMarketCalendar(),
    )
    failing_message = FakeMessage()
    failing_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=failing_message,
    )
    failing_context = SimpleNamespace(bot=FakeFailingBot(), args=[])

    asyncio.run(
        _handler_by_command(handlers, "check").callback(
            failing_update,
            failing_context,
        )
    )

    with open_connection(sqlite_path) as connection:
        failed_status = connection.execute(
            "SELECT notification_status FROM alert_events"
        ).fetchone()["notification_status"]

    success_message = FakeMessage()
    success_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=success_message,
    )
    success_context = SimpleNamespace(bot=FakeBot(), args=[])

    asyncio.run(
        _handler_by_command(handlers, "check").callback(
            success_update,
            success_context,
        )
    )

    with open_connection(sqlite_path) as connection:
        sent_status = connection.execute(
            "SELECT notification_status FROM alert_events"
        ).fetchone()["notification_status"]

    assert failed_status == "failed"
    assert "Notification delivery failures: 1." in failing_message.replies[0]
    assert sent_status == "sent"
    assert success_context.bot.messages == [
        {"chat_id": 456, "text": EXPECTED_DRAWDOWN_10_MESSAGE}
    ]


def test_profit_check_reuses_latest_for_same_code() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="159915",
            name="ChiNext ETF",
            asset_type=AssetType.CN_ETF.value,
            params={"cost": 1.85, "thresholds": [0.25]},
        )
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="159915",
            name="ChiNext ETF alt",
            asset_type=AssetType.CN_ETF.value,
            params={"cost": 1.90, "thresholds": [0.20]},
        )
        provider = FakeProvider(
            _history(["2024-01-01"], [100.0]),
            latest={"date": "2024-01-02", "close": 2.4, "source": "test"},
        )

        evaluate_profit_rules(connection, provider)
    finally:
        connection.close()

    assert len(provider.latest_calls) == 1


def test_list_shows_asset_type() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DRAW_DOWN_RULE_TYPE,
            symbol="110026",
            name="易方达创业板ETF联接A",
            asset_type=AssetType.CN_OPEN_FUND.value,
            params={
                "lookback_days": 365,
                "thresholds": [0.10, 0.15, 0.20],
                "price_field": "close",
            },
        )

        response = format_rules_list(list_rules(connection))
    finally:
        connection.close()

    assert "type=drawdown_from_high" in response
    assert "asset_type=cn_open_fund" in response
    assert "symbol=110026" in response


def test_list_shows_profit_rule() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="159915",
            name="ChiNext ETF",
            asset_type=AssetType.CN_ETF.value,
            params={"cost": 1.85, "thresholds": [0.25, 0.40]},
        )

        response = format_rules_list(list_rules(connection))
    finally:
        connection.close()

    assert "type=profit_reminder" in response
    assert "asset_type=cn_etf" in response
    assert "symbol=159915" in response
    assert 'params={"cost":1.85,"thresholds":[0.25,0.4]}' in response


def test_list_shows_dca_rule() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        add_rule(
            connection,
            type=DCA_RULE_TYPE,
            symbol="创业板",
            name="创业板",
            asset_type="dca",
            params={"weekday": "THU", "amount": 1000},
        )

        response = format_rules_list(list_rules(connection))
    finally:
        connection.close()

    assert "type=dca_reminder" in response
    assert "name=创业板" in response
    assert 'params={"amount":1000,"weekday":"THU"}' in response


def test_add_dca_command_persists_rule(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=FakeProvider(_history(["2024-01-02"], [1])),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=["创业板", "周四", "1000"])

    asyncio.run(_handler_by_command(handlers, "add_dca").callback(update, context))

    with open_connection(sqlite_path) as connection:
        rows = list_rules(connection)

    assert len(rows) == 1
    assert rows[0]["type"] == DCA_RULE_TYPE
    assert rows[0]["symbol"] == "创业板"
    assert rows[0]["name"] == "创业板"
    assert rows[0]["asset_type"] == "dca"
    assert message.replies == [
        "Added DCA rule id=1 name=创业板 weekday=THU amount=1000"
    ]


def test_set_dca_amount_updates_rule_without_replacing_it(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type=DCA_RULE_TYPE,
            symbol="科创50",
            name="科创50",
            asset_type="dca",
            params={"weekday": "THU", "amount": 300},
        )
    handlers = build_command_handlers({123}, sqlite_path=sqlite_path)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )

    asyncio.run(
        _handler_by_command(handlers, "set_dca_amount").callback(
            update, SimpleNamespace(args=[str(rule_id), "500"])
        )
    )

    with open_connection(sqlite_path) as connection:
        rules = list_rules(connection)
    assert len(rules) == 1
    assert int(rules[0]["id"]) == rule_id
    assert json.loads(rules[0]["params_json"])["amount"] == 500
    assert message.replies == [
        f"Updated DCA rule id={rule_id} 科创50 future amount to ¥500. "
        "Existing occurrences are unchanged."
    ]


def test_add_enhanced_dca_persists_settings_and_plans_instructions(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    provider = FakeProvider(_history(["2024-01-02"], [1]))
    provider.get_fund_type = lambda _symbol: "指数型-股票"
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(
        bot=FakeBot(),
        args=["110026", '"A500 feeder"', "周四", "2000", "rate:0.12%"],
    )

    asyncio.run(_handler_by_command(handlers, "add_dca").callback(update, context))
    asyncio.run(
        _handler_by_command(handlers, "plans").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=[]),
        )
    )

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]
        settings = get_fund_settings(connection, "110026")

    assert rule["asset_type"] == "cn_open_fund"
    assert rule["symbol"] == "110026"
    assert settings["fee_mode"] == "rate"
    assert settings["fee_value"] == pytest.approx(0.0012)
    assert "Position readiness: SETUP_REQUIRED" in message.replies[0]
    assert "Verified fund type: 指数型-股票" in message.replies[0]
    assert "A500 feeder (fixed DCA 1)" in message.replies[1]
    assert "remember /sync_position" in message.replies[1]


def test_add_enhanced_dca_rejects_qdii_metadata(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    provider = FakeProvider(_history(["2024-01-02"], [1]))
    provider.get_fund_type = lambda _symbol: "QDII-普通股票"
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()

    asyncio.run(
        _handler_by_command(handlers, "add_dca").callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
            ),
            SimpleNamespace(
                bot=FakeBot(),
                args=["110026", "Global", "周四", "2000", "rate:0%"],
            ),
        )
    )

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rules = list_rules(connection)

    assert rules == []
    assert "does not use the domestic CN valuation calendar" in message.replies[0]


def test_fund_setting_commands_persist_shared_values(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    handlers = build_command_handlers({123}, sqlite_path=sqlite_path)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=FakeMessage(),
    )

    asyncio.run(
        _handler_by_command(handlers, "set_fund_fee").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=["110026", "rate:0.15%"]),
        )
    )
    asyncio.run(
        _handler_by_command(handlers, "set_fund_cutoff").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=["110026", "14:45"]),
        )
    )

    with open_connection(sqlite_path) as connection:
        row = connection.execute(
            "SELECT * FROM fund_settings WHERE fund_symbol = '110026'"
        ).fetchone()

    assert row["fee_mode"] == "rate"
    assert row["fee_value"] == 0.0015
    assert row["subscription_cutoff"] == "14:45"
    assert "future estimates" in update.effective_message.replies[0]
    assert "future manual confirmations" in update.effective_message.replies[1]


def test_mark_added_records_one_pending_estimate_after_confirmation(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 80],
        expected_date=date(2024, 1, 2),
    )

    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_calendar=FakeMarketCalendar(),
        now_factory=lambda: datetime(2024, 1, 2, 6, 0, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )

    asyncio.run(
        _handler_by_command(handlers, "mark_added").callback(
            update,
            SimpleNamespace(args=[str(rule_id), "15"]),
        )
    )
    with open_connection(sqlite_path) as connection:
        upsert_fund_cutoff(
            connection,
            fund_symbol="000001",
            subscription_cutoff="13:00",
        )
    callback_data = message.reply_markups[0].inline_keyboard[0][0].callback_data
    query = FakeCallbackQuery(callback_data)
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
        callback_query=query,
    )
    callback = _callback_by_name(handlers, "manual_add_confirm_callback")
    asyncio.run(callback.callback(callback_update, SimpleNamespace()))
    asyncio.run(callback.callback(callback_update, SimpleNamespace()))

    with open_connection(sqlite_path) as connection:
        estimates = connection.execute("SELECT * FROM manual_add_estimates").fetchall()
        actions = connection.execute("SELECT * FROM manual_add_actions").fetchall()

    assert "Continue only if you already submitted" in message.replies[0]
    assert len(estimates) == 1
    assert estimates[0]["gross_amount"] == 5000
    assert estimates[0]["cutoff_time"] == "15:00"
    assert estimates[0]["effective_date"] == "2024-01-02"
    assert estimates[0]["status"] == "pending"
    assert len(actions) == 1
    assert actions[0]["tier_key"] == "0.15"
    assert all("waiting for exact dated NAV" in edit for edit in query.edits)


def test_mark_added_calendar_outage_returns_actionable_error(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 80],
        expected_date=date(2024, 1, 2),
    )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_calendar=FakeMarketCalendar(
            confirmed_error=MarketCalendarUnavailableError("calendar unavailable")
        ),
        now_factory=lambda: datetime(2024, 1, 2, 6, 0, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    asyncio.run(
        _handler_by_command(handlers, "mark_added").callback(
            update,
            SimpleNamespace(args=[str(rule_id), "15"]),
        )
    )
    query = FakeCallbackQuery(
        message.reply_markups[0].inline_keyboard[0][0].callback_data
    )
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=query,
    )
    asyncio.run(
        _callback_by_name(handlers, "manual_add_confirm_callback").callback(
            callback_update,
            SimpleNamespace(),
        )
    )

    with open_connection(sqlite_path) as connection:
        estimate_count = connection.execute(
            "SELECT COUNT(*) FROM manual_add_estimates"
        ).fetchone()[0]

    assert query.edits[-1] == "Addition was not recorded: calendar unavailable"
    assert estimate_count == 0


def test_mark_added_fallback_matches_earlier_same_day_multi_tier_alert(
    tmp_path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 80],
        expected_date=date(2024, 1, 2),
    )

    with open_connection(sqlite_path) as connection:
        original = connection.execute("SELECT * FROM alert_events").fetchone()
        payload = json.loads(str(original["payload_json"]))
        payload["crossed_tiers"] = payload["crossed_tiers"][:1]
        payload["total_amount"] = 5000
        connection.execute(
            """
            INSERT INTO alert_events (
                rule_id, alert_key, title, message, payload_json, triggered_at
            )
            VALUES (?, ?, 'close', 'close', ?, '2024-01-02T08:00:00Z')
            """,
            (rule_id, "later-subset", json.dumps(payload)),
        )
        connection.commit()

        selection = load_manual_add_selection(
            connection,
            plan_id=rule_id,
            action_date=date(2024, 1, 2),
            tier_keys=("0.15", "0.2"),
        )
        indexed = load_manual_add_selection(
            connection,
            plan_id=rule_id,
            event_id=int(original["id"]),
            action_date=date(2024, 1, 2),
            tier_index=1,
        )

    assert selection.event_id == original["id"]
    assert [tier.key for tier in selection.tiers] == ["0.15", "0.2"]
    assert [tier.key for tier in indexed.tiers] == ["0.2"]


def test_mark_added_fallback_exactly_matches_close_tier_keys(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 80],
        expected_date=date(2024, 1, 2),
        tiers=[
            {"drawdown": 0.1500000000001, "amount": 100},
            {"drawdown": 0.1500000000002, "amount": 200},
        ],
    )
    command = parse_mark_added_args([str(rule_id), "15.00000000001"])

    with open_connection(sqlite_path) as connection:
        selection = load_manual_add_selection(
            connection,
            plan_id=rule_id,
            action_date=date(2024, 1, 2),
            tier_keys=command.tier_keys,
        )

    assert [tier.key for tier in selection.tiers] == ["0.1500000000001"]


def test_mark_added_keeps_printed_high_precision_percentage_exact(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 10],
        expected_date=date(2024, 1, 2),
        tiers=[{"drawdown": 0.7949398787921911, "amount": 100}],
    )
    command = parse_mark_added_args([str(rule_id), "79.49398787921911"])

    with open_connection(sqlite_path) as connection:
        selection = load_manual_add_selection(
            connection,
            plan_id=rule_id,
            action_date=date(2024, 1, 2),
            tier_keys=command.tier_keys,
        )

    assert command.tier_keys == ("0.7949398787921911",)
    assert [tier.key for tier in selection.tiers] == ["0.7949398787921911"]


def test_mark_added_scientific_percentage_preserves_exact_tier_key() -> None:
    command = parse_mark_added_args(["7", "1E-98"])

    assert command.tier_keys == ("0." + "0" * 99 + "1",)


def test_mark_added_after_cutoff_uses_next_confirmed_open_day(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 100, 100, 100, 80],
        expected_date=date(2024, 1, 5),
    )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_calendar=FakeMarketCalendar(
            open_dates={date(2024, 1, 5), date(2024, 1, 8)}
        ),
        timezone="America/New_York",
        now_factory=lambda: datetime(2024, 1, 5, 9, 10, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    asyncio.run(
        _handler_by_command(handlers, "mark_added").callback(
            update,
            SimpleNamespace(args=[str(rule_id), "15"]),
        )
    )
    query = FakeCallbackQuery(
        message.reply_markups[0].inline_keyboard[0][0].callback_data
    )
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
        callback_query=query,
    )
    asyncio.run(
        _callback_by_name(handlers, "manual_add_confirm_callback").callback(
            callback_update,
            SimpleNamespace(),
        )
    )
    query.data = query.reply_markups[0].inline_keyboard[1][0].callback_data
    asyncio.run(
        _callback_by_name(handlers, "manual_add_cutoff_callback").callback(
            callback_update,
            SimpleNamespace(),
        )
    )

    with open_connection(sqlite_path) as connection:
        estimate = connection.execute("SELECT * FROM manual_add_estimates").fetchone()

    assert estimate["cutoff_choice"] == "after"
    assert estimate["effective_date"] == "2024-01-08"
    assert "2024-01-08" in query.edits[-1]


def test_sync_position_persists_exact_snapshot_and_shows_dated_value(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    provider = FakeProvider(
        _history(["2026-08-08"], [1.5]),
        nav=FundNav("110026", date(2026, 8, 8), 1.5, "akshare_eastmoney"),
    )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )

    asyncio.run(
        _handler_by_command(handlers, "sync_position").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=["110026", "1000", "1.2"]),
        )
    )

    with open_connection(sqlite_path) as connection:
        row = connection.execute(
            "SELECT * FROM position_snapshots WHERE fund_symbol = '110026'"
        ).fetchone()

    assert (row["units"], row["average_unit_cost"], row["is_estimated"]) == (
        1000,
        1.2,
        0,
    )
    assert provider.nav_calls == ["110026"]
    assert "Accuracy: exact" in message.replies[0]
    assert "Latest unit NAV: 1.5 on 2026-08-08" in message.replies[0]
    assert "Position value: ¥1,500.00" in message.replies[0]


def test_sync_none_included_keeps_unestimated_manual_add_sync_warning(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 84],
        expected_date=date(2024, 1, 2),
        tiers=[
            {"drawdown": 0.15, "amount": 5000.005},
            {"drawdown": 0.20, "amount": 10000},
        ],
    )
    with open_connection(sqlite_path) as connection:
        cycle = get_active_drawdown_cycle(connection, rule_id)
        event = connection.execute("SELECT id FROM alert_events").fetchone()
        record_manual_addition(
            connection,
            rule_id=rule_id,
            cycle_id=int(cycle["id"]),
            source_alert_event_id=int(event["id"]),
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000.005, "0.15"),),
            action_at=datetime(2024, 1, 2, 8, tzinfo=UTC),
            create_estimate=False,
        )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        now_factory=lambda: datetime(2024, 1, 3, 8, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    asyncio.run(
        _handler_by_command(handlers, "sync_position").callback(
            update,
            SimpleNamespace(args=["000001", "1100", "1.1"]),
        )
    )
    query = FakeCallbackQuery(
        message.reply_markups[0].inline_keyboard[1][0].callback_data
    )
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=query,
    )
    asyncio.run(
        _callback_by_name(handlers, "position_sync_callback").callback(
            callback_update,
            SimpleNamespace(),
        )
    )

    assert "¥5,000.005" in message.replies[0]
    assert "Choose None only if no item below is included" in message.replies[0]
    assert "still require another /sync_position" in query.edits[-1]
    assert "eligible for dated-NAV processing" not in query.edits[-1]


def test_sync_position_paginates_large_pending_preview(tmp_path, monkeypatch) -> None:
    pending_items = [
        {
            "key": f"action:{index}:0.15",
            "kind": "manual add requiring sync",
            "date": "2024-01-02",
            "amount": 5000,
        }
        for index in range(100)
    ]
    monkeypatch.setattr(
        "fund_alert_bot.commands.list_pending_position_items",
        lambda connection, fund_symbol: pending_items,
    )
    handlers = build_command_handlers(
        {123},
        sqlite_path=tmp_path / "fund_alert_bot.sqlite3",
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )

    asyncio.run(
        _handler_by_command(handlers, "sync_position").callback(
            update,
            SimpleNamespace(args=["000001", "1100", "1.1"]),
        )
    )

    assert len(message.replies) >= 3
    assert all(len(reply) <= 4096 for reply in message.replies)
    assert (
        sum(reply.count("manual add requiring sync") for reply in message.replies)
        == 100
    )
    assert "Review all 100 pending additions" in message.replies[-1]
    assert len(message.reply_markups) == 1


def test_partial_position_sync_changes_nothing(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    rule_id = _prepare_ready_plan_alert(
        sqlite_path,
        closes=[100, 84],
        expected_date=date(2024, 1, 2),
        tiers=[{"drawdown": 0.15, "amount": 5000}],
    )
    with open_connection(sqlite_path) as connection:
        cycle = get_active_drawdown_cycle(connection, rule_id)
        event = connection.execute("SELECT id FROM alert_events").fetchone()
        record_manual_addition(
            connection,
            rule_id=rule_id,
            cycle_id=int(cycle["id"]),
            source_alert_event_id=int(event["id"]),
            fund_symbol="000001",
            tiers=(DrawdownTier(0.15, 5000, "0.15"),),
            action_at=datetime(2024, 1, 2, 8, tzinfo=UTC),
            create_estimate=False,
        )
        before_position = dict(get_position_snapshot(connection, "000001"))

    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        now_factory=lambda: datetime(2024, 1, 3, 8, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    asyncio.run(
        _handler_by_command(handlers, "sync_position").callback(
            update,
            SimpleNamespace(args=["000001", "1100", "1.1"]),
        )
    )
    query = FakeCallbackQuery(
        message.reply_markups[0].inline_keyboard[2][0].callback_data
    )
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=query,
    )
    asyncio.run(
        _callback_by_name(handlers, "position_sync_callback").callback(
            callback_update,
            SimpleNamespace(),
        )
    )

    with open_connection(sqlite_path) as connection:
        position = dict(get_position_snapshot(connection, "000001"))
        pending = list_pending_position_items(connection, "000001")

    assert position == before_position
    assert len(pending) == 1
    assert "only some pending additions" in query.edits[-1]
    assert "Nothing was changed" in query.edits[-1]


def test_closed_position_does_not_request_eastmoney_nav(tmp_path) -> None:
    provider = FakeProvider(_history(["2026-08-08"], [1.5]))
    handlers = build_command_handlers(
        {123},
        sqlite_path=tmp_path / "fund_alert_bot.sqlite3",
        market_data_provider=provider,
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )

    asyncio.run(
        _handler_by_command(handlers, "sync_position").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=["110026", "0", "0"]),
        )
    )

    assert provider.nav_calls == []
    assert "Position value: ¥0.00 (closed)" in message.replies[0]


def test_drawdown_plan_preview_is_read_only_and_shows_total_and_readiness() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        command = parse_add_drawdown_plan_args(
            ["510300", "000001", "A500", "15:5000,20:10000"]
        )
        provider = FakeProvider(
            _plan_history([100, 80]),
            nav=FundNav("000001", date(2024, 1, 2), 1.2, "akshare_eastmoney"),
        )

        preview = build_drawdown_plan_preview(
            connection,
            provider,
            command,
            today=date(2024, 1, 2),
        )
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("rules", "drawdown_cycles", "alert_events")
        ]
    finally:
        connection.close()

    assert "Maximum one-cycle total: ¥15,000" in preview
    assert "Plan readiness: SETUP_REQUIRED" in preview
    assert "Current drawdown preview: -20.0%" in preview
    assert "Confirm only if these codes" in preview
    assert provider.price_bases == [PriceBasis.QFQ]
    assert counts == [0, 0, 0]


def test_add_drawdown_plan_requires_scoped_confirmation_and_is_idempotent(
    tmp_path,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    provider = FakeProvider(
        _plan_history([100, 90]),
        nav=FundNav("000001", date(2024, 1, 2), 1.2, "akshare_eastmoney"),
    )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
        now_factory=lambda: datetime(2024, 1, 2, 6, 0, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(
        bot=FakeBot(),
        args=["510300", "000001", '"A500 Core"', "15:5000,20:10000"],
    )

    asyncio.run(
        _handler_by_command(handlers, "add_drawdown_plan").callback(update, context)
    )
    with open_connection(sqlite_path) as connection:
        assert list_rules(connection) == []

    callback_data = message.reply_markups[0].inline_keyboard[0][0].callback_data
    query = FakeCallbackQuery(callback_data)
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
        callback_query=query,
    )
    callback = _drawdown_plan_callback(handlers)
    asyncio.run(callback.callback(callback_update, SimpleNamespace()))
    asyncio.run(callback.callback(callback_update, SimpleNamespace()))

    with open_connection(sqlite_path) as connection:
        rules = list_rules(connection)
        cycle_count = connection.execute(
            "SELECT COUNT(*) FROM drawdown_cycles"
        ).fetchone()[0]

    assert len(rules) == 1
    assert rules[0]["type"] == "drawdown_plan"
    assert rules[0]["symbol"] == "510300"
    assert json.loads(rules[0]["params_json"])["investment_fund_symbol"] == "000001"
    assert cycle_count == 0
    assert query.answer_count == 2
    assert all("Saved Drawdown Add Plan id=1" in text for text in query.edits)


def test_drawdown_plan_confirmation_expires_without_saving(tmp_path) -> None:
    current = [datetime(2024, 1, 2, 6, 0, tzinfo=UTC)]
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    provider = FakeProvider(
        _plan_history([100, 90]),
        nav=FundNav("000001", date(2024, 1, 2), 1.2, "akshare_eastmoney"),
    )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
        now_factory=lambda: current[0],
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    asyncio.run(
        _handler_by_command(handlers, "add_drawdown_plan").callback(
            update,
            SimpleNamespace(
                bot=FakeBot(),
                args=["510300", "000001", "A500", "15:5000"],
            ),
        )
    )
    current[0] = datetime(2024, 1, 2, 6, 11, tzinfo=UTC)
    query = FakeCallbackQuery(
        message.reply_markups[0].inline_keyboard[0][0].callback_data
    )

    asyncio.run(
        _drawdown_plan_callback(handlers).callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
                callback_query=query,
            ),
            SimpleNamespace(),
        )
    )

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        assert list_rules(connection) == []
    assert "expired" in query.edits[0]


def test_conflicting_plan_is_rejected_before_any_market_request(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="existing",
            asset_type="cn_etf",
            params={"investment_fund_symbol": "000001"},
        )
    provider = FakeProvider(_plan_history([100, 90]))
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()

    asyncio.run(
        _handler_by_command(handlers, "add_drawdown_plan").callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
            ),
            SimpleNamespace(
                bot=FakeBot(),
                args=["510300", "000002", "duplicate", "15:5000"],
            ),
        )
    )

    assert "Plan conflict" in message.replies[0]
    assert provider.calls == []
    assert provider.nav_calls == []


def test_plans_and_check_show_plan_state_without_mutation(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="A500",
            asset_type="cn_etf",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "tiers": [
                    {"drawdown": 0.15, "amount": 5000},
                    {"drawdown": 0.20, "amount": 10000},
                ],
                "sma_window": 250,
                "sma_slope_window": 20,
            },
        )
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="000009",
            name="broken",
            asset_type=AssetType.CN_OPEN_FUND.value,
            params={"cost": "auto"},
        )
    provider = FakeProvider(_plan_history([100, 80]))
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
        now_factory=lambda: datetime(2024, 1, 2, 9, 0, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )

    asyncio.run(
        _handler_by_command(handlers, "plans").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=[]),
        )
    )
    setup_data = message.reply_markups[0].inline_keyboard[0][0].callback_data
    setup_query = FakeCallbackQuery(setup_data)
    asyncio.run(
        _callback_by_name(handlers, "position_profit_setup_callback").callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
                callback_query=setup_query,
            ),
            SimpleNamespace(),
        )
    )
    asyncio.run(
        _handler_by_command(handlers, "check").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=[]),
        )
    )

    with open_connection(sqlite_path) as connection:
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("drawdown_cycles", "drawdown_tier_records", "alert_events")
        ]

    assert "Drawdown: 0.0%" in message.replies[0]
    assert "Template: /add_profit cn_open_fund 000001 A500 auto" in message.replies[0]
    assert setup_data == "profit_setup:000001"
    assert "/add_profit cn_open_fund 000001 A500 auto" in setup_query.edits[0]
    assert "No rule was created by this button." in setup_query.edits[0]
    assert "Next open tier: -15% / ¥5,000" in message.replies[0]
    assert "awaiting official close confirmation" not in message.replies[0]
    assert "Drawdown Add Plan status (read-only)" in message.replies[1]
    assert "Read-only Drawdown Add Plans checked: 1" in message.replies[1]
    assert "No enabled drawdown_from_high" not in message.replies[1]
    assert "• -15% / ¥5,000: open" in message.replies[1]
    assert counts == [0, 0, 0]


def test_plans_keeps_position_linked_when_plan_market_data_fails(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="A500",
            asset_type="cn_etf",
            params={
                "investment_fund_symbol": "000001",
                "tiers": [{"drawdown": 0.15, "amount": 5000}],
            },
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=0,
            average_unit_cost=0,
        )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=FakeProvider(_history(["2024-01-02"], [100])),
        now_factory=lambda: datetime(2024, 1, 2, 9, 0, tzinfo=UTC),
    )
    message = FakeMessage()

    asyncio.run(
        _handler_by_command(handlers, "plans").callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
            ),
            SimpleNamespace(bot=FakeBot(), args=[]),
        )
    )

    assert (
        "Drawdown Add Plan configured; market status unavailable" in message.replies[0]
    )
    assert "no enabled Drawdown Add Plan" not in message.replies[0]


def test_add_profit_command_persists_rule(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    handlers = build_command_handlers({123}, sqlite_path=sqlite_path)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(
        bot=FakeBot(),
        args=["cn_etf", "159915", "ChiNext ETF", "1.85", "25,40"],
    )

    asyncio.run(_handler_by_command(handlers, "add_profit").callback(update, context))

    with open_connection(sqlite_path) as connection:
        rows = list_rules(connection)

    assert len(rows) == 1
    assert rows[0]["type"] == PROFIT_RULE_TYPE
    assert rows[0]["symbol"] == "159915"
    assert rows[0]["name"] == "ChiNext ETF"
    assert rows[0]["asset_type"] == AssetType.CN_ETF.value
    assert json.loads(rows[0]["params_json"]) == {
        "cost": 1.85,
        "thresholds": [0.25, 0.40],
    }
    assert message.replies == [
        (
            "Added profit rule id=1 asset_type=cn_etf "
            "symbol=159915 name=ChiNext ETF cost=1.85"
        )
    ]


def test_auto_profit_preview_defers_pending_position_sync(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
        connection.execute(
            "UPDATE position_snapshots SET position_sync_required_since = ? "
            "WHERE fund_symbol = ?",
            ("2024-01-02T06:00:00+00:00", "000001"),
        )
        connection.commit()
    provider = FakeProvider(
        _history(["2024-01-02"], [1.3]),
        nav=FundNav("000001", date(2024, 1, 2), 1.3, "akshare_eastmoney"),
    )
    provider.get_fund_type = lambda _symbol: "指数型-股票"
    handlers = build_command_handlers(
        {123}, sqlite_path=sqlite_path, market_data_provider=provider
    )
    message = FakeMessage()
    asyncio.run(
        _handler_by_command(handlers, "add_profit").callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
            ),
            SimpleNamespace(
                bot=FakeBot(),
                args=["cn_open_fund", "000001", "A500", "auto", "20,30"],
            ),
        )
    )
    asyncio.run(
        _handler_by_command(handlers, "plans").callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
            ),
            SimpleNamespace(bot=FakeBot(), args=[]),
        )
    )

    assert (
        "Read-only preview unavailable: Position Sync is required."
        in message.replies[0]
    )
    assert "Position Sync required — reminders paused" in message.replies[1]
    assert provider.nav_calls == []


def test_auto_profit_preview_and_position_actions(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=100,
            average_unit_cost=1,
        )
    provider = FakeProvider(
        _history(["2024-01-02"], [1.3]),
        nav=FundNav("000001", date(2024, 1, 2), 1.3, "akshare_eastmoney"),
    )
    provider.get_fund_type = lambda _symbol: "指数型-股票"
    calendar = FakeMarketCalendar(open_dates={date(2024, 1, 2)})
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
        market_calendar=calendar,
        now_factory=lambda: datetime(2024, 1, 3, 8, 30, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    asyncio.run(
        _handler_by_command(handlers, "add_profit").callback(
            update,
            SimpleNamespace(
                bot=FakeBot(),
                args=["cn_open_fund", "000001", "A500", "auto", "20,30"],
            ),
        )
    )

    with open_connection(sqlite_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM position_profit_thresholds"
            ).fetchone()[0]
            == 0
        )
        result = evaluate_position_profit_rules(
            connection,
            provider,
            calendar,
            processing_date=date(2024, 1, 3),
        )
        event_id = result.notifications[0].event_id
    assert "2024-01-02 (akshare_eastmoney)" in message.replies[0]
    assert "Preview only; no threshold was consumed" in message.replies[0]

    active_plans_message = FakeMessage()
    update.effective_message = active_plans_message
    asyncio.run(
        _handler_by_command(handlers, "plans").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=[]),
        )
    )
    assert "Position: exact; last sync" in active_plans_message.replies[0]
    assert "Position value: ¥130.00" in active_plans_message.replies[0]

    partial_query = FakeCallbackQuery(f"profit_action:{event_id}:partial")
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=partial_query,
    )
    asyncio.run(
        _callback_by_name(handlers, "position_profit_action_callback").callback(
            callback_update,
            SimpleNamespace(),
        )
    )
    assert "/sync_position 000001 <remaining_units>" in partial_query.edits[0]
    with open_connection(sqlite_path) as connection:
        assert get_position_snapshot(connection, "000001")["units"] == 100

    close_query = FakeCallbackQuery(f"profit_action:{event_id}:close")
    callback_update.callback_query = close_query
    action_handler = _callback_by_name(handlers, "position_profit_action_callback")
    asyncio.run(action_handler.callback(callback_update, SimpleNamespace()))
    assert close_query.reply_markups
    confirm_data = close_query.reply_markups[0].inline_keyboard[0][0].callback_data
    confirm_query = FakeCallbackQuery(confirm_data)
    callback_update.callback_query = confirm_query
    asyncio.run(
        _callback_by_name(handlers, "position_profit_close_callback").callback(
            callback_update,
            SimpleNamespace(),
        )
    )
    with open_connection(sqlite_path) as connection:
        assert get_position_snapshot(connection, "000001")["units"] == 0
        assert get_active_position_cycle(connection, "000001") is None

    closed_plans_message = FakeMessage()
    update.effective_message = closed_plans_message
    asyncio.run(
        _handler_by_command(handlers, "plans").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=[]),
        )
    )
    assert "Position: closed (exact zero units)" in closed_plans_message.replies[0]
    assert "remember /sync_position" not in closed_plans_message.replies[0]


def test_auto_profit_rejects_qdii_before_saving(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    provider = FakeProvider(_history(["2024-01-02"], [1.3]))
    provider.get_fund_type = lambda _symbol: "QDII-普通股票"
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()
    asyncio.run(
        _handler_by_command(handlers, "add_profit").callback(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=456),
                effective_message=message,
            ),
            SimpleNamespace(
                bot=FakeBot(),
                args=["cn_open_fund", "000001", "QDII", "auto", "20"],
            ),
        )
    )

    with open_connection(sqlite_path) as connection:
        init_db(connection)
        assert list_rules(connection) == []
    assert "does not use the domestic CN valuation calendar" in message.replies[0]


def test_delete_command_reports_disabled_drawdown_plan(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="A500",
            asset_type=AssetType.CN_ETF.value,
            params={"investment_fund_symbol": "000001", "tiers": []},
        )

    handlers = build_command_handlers({123}, sqlite_path=sqlite_path)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[str(rule_id)])

    asyncio.run(_handler_by_command(handlers, "del").callback(update, context))

    with open_connection(sqlite_path) as connection:
        rule = list_rules(connection)[0]

    assert rule["enabled"] == 0
    assert "status=disabled" in format_rules_list([rule])
    assert message.replies == [f"Disabled drawdown plan id={rule_id}"]


@pytest.mark.parametrize("malformed", [False, True])
def test_delete_command_keeps_legacy_open_fund_behavior(
    tmp_path,
    malformed: bool,
) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="000001",
            name="A500",
            asset_type=AssetType.CN_OPEN_FUND.value,
            params={"cost": 1.2, "thresholds": [0.2]},
        )
        if malformed:
            connection.execute(
                "UPDATE rules SET params_json = '{' WHERE id = ?", (rule_id,)
            )
            connection.commit()

    handlers = build_command_handlers({123}, sqlite_path=sqlite_path)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    asyncio.run(
        _handler_by_command(handlers, "del").callback(
            update,
            SimpleNamespace(bot=FakeBot(), args=[str(rule_id)]),
        )
    )

    with open_connection(sqlite_path) as connection:
        assert list_rules(connection) == []
    assert message.replies == [f"Deleted rule id={rule_id}"]


def test_check_sends_due_dca_without_market_data_fetch(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type=DCA_RULE_TYPE,
            symbol="创业板",
            name="创业板",
            asset_type="dca",
            params={"weekday": weekday_for_date(date.today()), "amount": 1000},
        )

    provider = FakeProvider(_history(["2024-01-01"], [100.0]))
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[])

    asyncio.run(_handler_by_command(handlers, "check").callback(update, context))

    assert provider.calls == []
    expected_dca_message = EXPECTED_DCA_MESSAGE.replace(
        "2024-01-04",
        date.today().isoformat(),
    )
    assert context.bot.messages == [
        {
            "chat_id": 456,
            "text": expected_dca_message,
        }
    ]
    assert "Checked 1 dca_reminder rule(s)." in message.replies[0]


def test_check_evaluates_profit_rules_with_latest_data(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="159915",
            name="ChiNext ETF",
            asset_type=AssetType.CN_ETF.value,
            params={"cost": 1.85, "thresholds": [0.25, 0.40]},
        )

    provider = FakeProvider(
        _history(["2024-01-01"], [100.0]),
        latest={"date": "2024-01-02", "close": 2.4, "source": "test"},
    )
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
        now_factory=lambda: datetime(2024, 1, 2, 6, 0, tzinfo=UTC),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[])

    asyncio.run(_handler_by_command(handlers, "check").callback(update, context))

    assert [call.asset_type for call in provider.latest_calls] == [AssetType.CN_ETF]
    assert context.bot.messages == [
        {
            "chat_id": 456,
            "text": EXPECTED_PROFIT_MESSAGE,
        }
    ]
    assert "Checked 1 profit_reminder rule(s)." in message.replies[0]
    assert "New alerts: 1." in message.replies[0]


def test_check_reports_unavailable_latest_profit_data(tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        add_rule(
            connection,
            type=PROFIT_RULE_TYPE,
            symbol="110026",
            name="Example Fund",
            asset_type=AssetType.CN_OPEN_FUND.value,
            params={"cost": 1.0, "thresholds": [0.25]},
        )

    provider = FakeProvider(_history(["2024-01-01"], [100.0]), latest=None)
    handlers = build_command_handlers(
        {123},
        sqlite_path=sqlite_path,
        market_data_provider=provider,
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[])

    asyncio.run(_handler_by_command(handlers, "check").callback(update, context))

    assert context.bot.messages == []
    assert "No-data skips: 1." in message.replies[0]
    assert (
        "Rule 1 110026: Latest unit NAV is unavailable for 110026."
        in (message.replies[0])
    )


def test_test_notify_sends_to_enabled_channels(monkeypatch) -> None:
    webhook_calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> object:
        webhook_calls.append({"url": url, **kwargs})
        return FakeResponse(status_code=200)

    monkeypatch.setattr(
        "fund_alert_bot.notifications.webhook.requests.post",
        fake_post,
    )
    handlers = build_command_handlers(
        {123},
        notification_settings=NotificationSettings(
            webhook_enabled=True,
            webhook_url="https://hooks.example.test/secret",
        ),
    )
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=message,
    )
    context = SimpleNamespace(bot=FakeBot(), args=[])

    asyncio.run(handlers[-1].callback(update, context))

    assert context.bot.messages == [{"chat_id": 456, "text": TEST_NOTIFICATION_MESSAGE}]
    assert webhook_calls == [
        {
            "url": "https://hooks.example.test/secret",
            "json": {
                "title": "fund-alert-bot test",
                "body": TEST_NOTIFICATION_MESSAGE,
            },
            "timeout": 10,
        }
    ]
    assert message.replies == ["Sent test notification to 2 channel(s)."]


class FakeProvider:
    def __init__(
        self,
        history: pd.DataFrame,
        *,
        latest: dict[str, object] | None = None,
        nav: FundNav | None = None,
    ) -> None:
        self.history = history
        self.latest = latest
        self.nav = nav
        self.calls: list[tuple[Instrument, object, object]] = []
        self.latest_calls: list[Instrument] = []
        self.nav_calls: list[str] = []
        self.price_bases: list[PriceBasis] = []

    def get_history(
        self,
        instrument: Instrument,
        start_date: object,
        end_date: object,
        *,
        price_basis: PriceBasis = PriceBasis.UNADJUSTED,
    ) -> pd.DataFrame:
        self.calls.append((instrument, start_date, end_date))
        self.price_bases.append(price_basis)
        return self.history

    def get_latest(self, instrument: Instrument) -> dict[str, object] | None:
        self.latest_calls.append(instrument)
        return self.latest

    def get_fund_nav(
        self,
        instrument: Instrument,
        nav_date: object | None = None,
    ) -> FundNav:
        del nav_date
        self.nav_calls.append(instrument.symbol)
        if self.nav is None:
            raise AssertionError("unexpected fund NAV request")
        return self.nav


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_markups: list[object] = []
        self.reply_calls: list[tuple[str, object | None]] = []

    async def reply_text(
        self, text: str, *, reply_markup: object | None = None
    ) -> None:
        self.replies.append(text)
        self.reply_calls.append((text, reply_markup))
        if reply_markup is not None:
            self.reply_markups.append(reply_markup)


class FakeCallbackQuery:
    def __init__(self, data: str, *, reply_markup: object | None = None) -> None:
        self.data = data
        self.message = SimpleNamespace(reply_markup=reply_markup)
        self.answer_count = 0
        self.edits: list[str] = []
        self.reply_markups: list[object] = []

    async def answer(self) -> None:
        self.answer_count += 1

    async def edit_message_text(
        self,
        text: str,
        *,
        reply_markup: object | None = None,
    ) -> None:
        self.edits.append(text)
        self.message.reply_markup = reply_markup
        if reply_markup is not None:
            self.reply_markups.append(reply_markup)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})


class FakeCommandMenuBot:
    def __init__(self) -> None:
        self.commands: tuple[tuple[str, str], ...] = ()
        self.fail = False

    async def set_my_commands(self, commands: tuple[tuple[str, str], ...]) -> None:
        if self.fail:
            from telegram.error import TelegramError

            raise TelegramError("unavailable")
        self.commands = commands


class FakeFailingBot:
    async def send_message(self, *, chat_id: int, text: str) -> None:
        del chat_id, text
        raise RuntimeError("telegram unavailable")


class FakeResponse:
    def __init__(self, *, status_code: int) -> None:
        self.status_code = status_code


class FakeMarketCalendar:
    def __init__(
        self,
        open_dates: set[date] | None = None,
        confirmed_error: Exception | None = None,
    ) -> None:
        self.open_dates = open_dates
        self.confirmed_error = confirmed_error

    def confirmed_status(self, check_date: date) -> bool:
        if self.confirmed_error is not None:
            raise self.confirmed_error
        return self.open_dates is None or check_date in self.open_dates


def _history(dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(closes),
            "amount": [10000] * len(closes),
            "source": ["test"] * len(closes),
        }
    )


def _plan_history(closes: list[float]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(closes)),
            "close": closes,
            "source": ["akshare_eastmoney"] * len(closes),
        }
    )
    frame.attrs.update(
        {
            "symbol": "510300",
            "source": "akshare_eastmoney",
            "price_basis": "qfq",
            "frequency": "daily",
        }
    )
    return frame


def _handler_by_command(handlers: list[object], command: str) -> object:
    for handler in handlers:
        if command in getattr(handler, "commands", ()):
            return handler
    raise AssertionError(f"handler not found: {command}")


def test_merged_dca_skip_callback_preserves_other_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    monkeypatch.setattr(
        "fund_alert_bot.commands.skip_scheduled_dca_occurrence",
        lambda *_args, **_kwargs: "skipped",
    )
    handlers = build_command_handlers({123}, sqlite_path=sqlite_path)
    first_data = "dca_skip:1:2024-01-04"
    second_data = "dca_skip:2:2024-01-04"
    markup = InlineKeyboardMarkup(
        (
            (InlineKeyboardButton("First", callback_data=first_data),),
            (InlineKeyboardButton("Second", callback_data=second_data),),
        )
    )
    query = FakeCallbackQuery(first_data, reply_markup=markup)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=query,
    )

    asyncio.run(
        _callback_by_name(handlers, "dca_skip_callback").callback(
            update, SimpleNamespace()
        )
    )

    assert query.edits == ["Skipped fixed DCA occurrence 1 / 2024-01-04."]
    assert [
        button.callback_data
        for row in query.message.reply_markup.inline_keyboard
        for button in row
    ] == [second_data]


def _drawdown_plan_callback(handlers: list[object]) -> object:
    for handler in handlers:
        if handler.__class__.__name__ == "CallbackQueryHandler":
            return handler
    raise AssertionError("drawdown plan callback handler not found")


def _callback_by_name(handlers: list[object], name: str) -> object:
    for handler in handlers:
        callback = getattr(handler, "callback", None)
        if getattr(callback, "__name__", "") == name:
            return handler
    raise AssertionError(f"callback handler not found: {name}")


def _prepare_ready_plan_alert(
    sqlite_path: object,
    *,
    closes: list[float],
    expected_date: date,
    tiers: list[dict[str, float | int]] | None = None,
) -> int:
    with open_connection(sqlite_path) as connection:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="A500",
            asset_type="cn_etf",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 365,
                "tiers": tiers
                if tiers is not None
                else [
                    {"drawdown": 0.15, "amount": 5000},
                    {"drawdown": 0.20, "amount": 10000},
                ],
                "sma_window": 250,
                "sma_slope_window": 20,
            },
        )
        upsert_fund_fee(
            connection,
            fund_symbol="000001",
            fee_mode="rate",
            fee_value=0.0015,
        )
        upsert_position_snapshot(
            connection,
            fund_symbol="000001",
            units=1000,
            average_unit_cost=1.2,
        )
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _plan_history(closes),
            expected_date=expected_date,
        )
    assert result.notification is not None
    return rule_id
