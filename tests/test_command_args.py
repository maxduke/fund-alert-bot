from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from fund_alert_bot.command_args import (
    CommandParseError,
    dca_params,
    drawdown_params,
    parse_add_dca_args,
    parse_add_drawdown_args,
    parse_add_drawdown_plan_args,
    parse_add_profit_args,
    parse_mark_added_args,
    parse_set_fund_cutoff_args,
    parse_set_fund_fee_args,
    parse_set_plan_rearm_args,
    parse_sync_position_args,
    parse_thresholds,
    profit_params,
)
from fund_alert_bot.market_data import AssetType


def test_parsing_module_does_not_load_command_shell_or_storage() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from fund_alert_bot.command_args import parse_add_dca_args; "
            "assert parse_add_dca_args(['Core', 'MON', '100']).amount == 100; "
            "assert not {'fund_alert_bot.commands', 'fund_alert_bot.db', "
            "'telegram'} & sys.modules.keys()",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_command_imports_preserve_types_and_error_handling() -> None:
    from fund_alert_bot import command_args, commands

    # Callers may still import parser results/errors from the original module.
    for name, value in vars(command_args).items():
        if (
            name.endswith("_USAGE")
            or getattr(value, "__module__", None) == command_args.__name__
        ):
            assert getattr(commands, name) is value
    parsed = command_args.parse_add_drawdown_args(
        ["cn_etf", "510300", "Core", "365", "15,20"]
    )
    assert isinstance(parsed, commands.DrawdownCommand)
    with pytest.raises(commands.CommandParseError, match="positive"):
        command_args.parse_add_dca_args(["Core", "MON", "-1"])


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


def test_parse_drawdown_command_recovers_telegram_split_quoted_name() -> None:
    command = parse_add_drawdown_args(
        ["cn_index", "399006", '"ChiNext', 'Index"', "365", "10,15"]
    )

    assert command.name == "ChiNext Index"


@pytest.mark.parametrize(
    ("parser", "args"),
    (
        (
            parse_add_drawdown_args,
            ["cn_index", "399006", "Investor's", "365", "10,15"],
        ),
        (
            parse_add_profit_args,
            ["cn_open_fund", "110026", "Investor's", "auto", "20,30"],
        ),
        (parse_add_dca_args, ["Investor's", "周四", "1000"]),
    ),
)
def test_add_commands_preserve_literal_name_apostrophes(parser, args) -> None:
    assert parser(args).name == "Investor's"


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


def test_parse_profit_command_recovers_telegram_split_quoted_name() -> None:
    command = parse_add_profit_args(
        ["cn_open_fund", "110026", '"A500', 'feeder"', "auto", "20,30"]
    )

    assert command.name == "A500 feeder"


def test_parse_valid_dca_command_with_chinese_weekday() -> None:
    command = parse_add_dca_args(["创业板", "周四", "1000"])

    assert command.name == "创业板"
    assert command.weekday == "THU"
    assert command.amount == 1000
    assert dca_params(command) == {"weekday": "THU", "amount": 1000}


def test_parse_simple_dca_recovers_telegram_split_quoted_name() -> None:
    command = parse_add_dca_args(['"Core', 'fund"', "周四", "1000"])

    assert command.name == "Core fund"


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


def test_parse_enhanced_dca_recovers_telegram_split_quoted_name() -> None:
    command = parse_add_dca_args(
        ["110026", '"A500', 'feeder"', "周四", "2000", "rate:0.12%"]
    )

    assert command.name == "A500 feeder"
    assert command.holiday_policy == "next"


def test_parse_enhanced_dca_rejects_fixed_fee_consuming_amount() -> None:
    with pytest.raises(CommandParseError, match="lower than"):
        parse_add_dca_args(
            ["110026", "A500", "周四", "100", "fixed:100", "holiday:skip"]
        )


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


@pytest.mark.parametrize(
    "args",
    (
        ["510300", "000001", '"A500 Core"', "15:5000", "rearm:4%"],
        ["510300", "000001", '"A500 Core"', "15:5000", "rearm:4"],
        [
            "510300",
            "000001",
            '"A500 Core"',
            "15:5000",
            "lookback:730",
            "rearm:4%",
        ],
        [
            "510300",
            "000001",
            '"A500 Core"',
            "15:5000",
            "rearm:4%",
            "lookback:730",
        ],
    ),
)
def test_parse_drawdown_plan_rearm_options(args: list[str]) -> None:
    command = parse_add_drawdown_plan_args(args)

    assert command.config.rearm_margin == pytest.approx(0.04)
    assert command.params["rearm_margin"] == pytest.approx(0.04)
    if "lookback:730" in args:
        assert command.config.lookback_days == 730


@pytest.mark.parametrize(
    "args",
    (
        ["510300", "000001", "A500", "15:5000", "rearm:0"],
        ["510300", "000001", "A500", "15:5000", "rearm:-1"],
        ["510300", "000001", "A500", "15:5000", "rearm:100"],
        ["510300", "000001", "A500", "15:5000", "rearm:nan"],
        ["510300", "000001", "A500", "15:5000", "rearm:inf"],
        ["510300", "000001", "A500", "15:5000", "rearm:4", "rearm:5"],
        ["510300", "000001", "A500", "15:5000", "lookback:365", "lookback:730"],
        ["510300", "000001", "A500", "15:5000", "unknown:1"],
    ),
)
def test_parse_drawdown_plan_rejects_invalid_rearm_options(args: list[str]) -> None:
    with pytest.raises(CommandParseError):
        parse_add_drawdown_plan_args(args)


@pytest.mark.parametrize("raw", ["4", "4%"])
def test_parse_set_plan_rearm_args(raw: str) -> None:
    command = parse_set_plan_rearm_args(["5", raw])

    assert command.plan_id == 5
    assert command.rearm_margin == pytest.approx(0.04)


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
    historical = parse_mark_added_args(["12", "15,20", "2024-01-02"])
    assert historical.action_date == date(2024, 1, 2)


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
