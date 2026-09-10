from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from fund_alert_bot.checks import (
    DrawdownPlanStatus,
    DrawdownPlanStatusResult,
    RuleCheckError,
    RuleNoDataSkip,
)
from fund_alert_bot.command_args import parse_add_drawdown_plan_args
from fund_alert_bot.i18n import get_language, localize_text, set_language
from fund_alert_bot.market_data.models import FundNav
from fund_alert_bot.plan_views import format_plan_details, format_plan_overview
from fund_alert_bot.rules.drawdown_plan import DrawdownPlanEvaluation


@pytest.fixture
def snapshot():
    config = parse_add_drawdown_plan_args(
        ["510300", "110026", "Core", "15:100,20:200,25:300,30:400,35:500"]
    ).config
    evaluation = DrawdownPlanEvaluation(
        latest_date=date(2024, 1, 3),
        latest_price=65,
        initial_peak_date=date(2024, 1, 1),
        initial_peak_price=100,
        peak_date=date(2024, 1, 1),
        peak_price=100,
        drawdown=0.35,
        sma=None,
        above_sma=None,
        distance_to_sma=None,
        sma_slope=None,
        source="akshare_eastmoney",
        coverage_start=date(2024, 1, 1),
        cycle_initialized=False,
        cycle_changed=False,
        newly_crossed_tiers=(),
        total_amount=0,
    )
    status = DrawdownPlanStatus(
        rule_id=7,
        reference_symbol="510300",
        name="Core",
        config=config,
        evaluation=evaluation,
        recorded_tier_keys=frozenset({"0.15", "0.2", "0.25"}),
        added_tier_keys=frozenset({"0.15"}),
        skipped_tier_keys=frozenset({"0.25"}),
        snoozed_tier_keys=frozenset({"0.2"}),
        readiness="READY",
        missing_setup=(),
        position={
            "units": 1000,
            "is_estimated": True,
            "last_synced_at": "2024-01-01",
            "estimates_since_sync": 2,
        },
        fund_nav=FundNav("110026", date(2024, 1, 2), 1.2, "akshare_eastmoney"),
        position_sync_required_since=None,
    )
    language = get_language()
    set_language("en")
    try:
        yield DrawdownPlanStatusResult(1, [status], [], [])
    finally:
        set_language(language)


@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_tier_actions_dates_and_positions_remain_distinct_without_mutation(
    snapshot, language
):
    set_language(language)
    before = copy.deepcopy(snapshot)
    overview = localize_text(format_plan_overview(snapshot))
    details = localize_text(format_plan_details(snapshot))
    for state in ("ADDED", "TRIGGERED_PENDING", "SKIPPED", "UNTRIGGERED"):
        assert state in details
    assert "/mark_added 7 20 <YYYY-MM-DD>" in overview
    assert "2024-01-03" in overview and "2024-01-02" in overview
    assert "1,200.00" in details and "1.2" in details
    assert "Core" in overview and "Core" in details
    if language == "en":
        assert "deferred today" in details
        assert (
            "Reached, awaiting official close confirmation: -30% / ¥400, -35% / ¥500"
            in overview
        )
        assert "Next open tier: -30% / ¥400" in overview
        assert "Position: estimated" in details
    else:
        assert "今天已顺延提醒" in details
        assert "已加仓" in details and "已跳过" in details
    assert snapshot == before


@pytest.mark.parametrize("kind", ["missing", "closed", "missing_nav", "reconcile"])
def test_position_boundaries_are_formatted_from_supplied_snapshot(snapshot, kind):
    status = snapshot.statuses[0]
    if kind == "missing":
        status = replace(status, position=None)
        expected = "Position: not synced"
    elif kind == "closed":
        status = replace(status, position={**status.position, "units": 0})
        expected = "Position value: ¥0.00 (closed)"
    elif kind == "missing_nav":
        status = replace(status, fund_nav=None, fund_nav_unavailable_reason="stale NAV")
        expected = "Position value: unavailable: stale NAV"
    else:
        status = replace(status, position_sync_required_since="2024-01-02")
        expected = "Position sync required since 2024-01-02"
    result = replace(snapshot, statuses=[status])
    before = copy.deepcopy(result)
    assert expected in format_plan_overview(result)
    assert expected in format_plan_details(result)
    assert result == before


def test_no_negative_zero_or_invented_sma_on_partial_history(snapshot):
    status = snapshot.statuses[0]
    status = replace(status, evaluation=replace(status.evaluation, drawdown=0))
    result = replace(snapshot, statuses=[status])
    for text in (format_plan_overview(result), format_plan_details(result)):
        assert "0.0%" in text and "-0.0%" not in text
    details = format_plan_details(result)
    assert "MA250: unavailable (insufficient history)" in details
    assert "Current: 65" in details
    assert "Rearm threshold: 102" in details


def test_failed_plans_remain_visible_without_fabricating_positions():
    result = DrawdownPlanStatusResult(
        2,
        [],
        [RuleNoDataSkip(1, "510300", "missing close")],
        [RuleCheckError(2, "510500", "calendar unavailable")],
    )
    for text in (format_plan_overview(result), format_plan_details(result)):
        assert "510300" in text and "missing close" in text
        assert "510500" in text and "calendar unavailable" in text
        assert "Position:" not in text
    empty = DrawdownPlanStatusResult(0, [], [], [])
    assert format_plan_overview(empty) == "No investment plans or positions configured."
    assert format_plan_details(empty) == ""


def test_dca_overview_retains_conditional_skip_and_effective_date():
    row = {
        "params_json": json.dumps(
            {"weekday": "MON", "amount": 1000, "holiday_policy": "next"}
        ),
        "name": "DCA",
        "rule_id": 2,
        "fund_symbol": "110026",
        "due_date": "2024-01-01",
        "effective_date": None,
        "status": "pending",
        "added_units": None,
        "last_synced_at": None,
    }
    before = copy.deepcopy(row)
    text = format_plan_overview(
        DrawdownPlanStatusResult(0, [], [], []), dca_statuses=[row]
    )
    assert "NAV date unresolved" in text
    assert "If the deduction failed, use:" in text
    assert "/dca_skip 2 2024-01-01" in text
    assert "Position: not synced" in text
    assert row == before


def test_malformed_profit_overview_keeps_existing_warning_category(caplog):
    text = format_plan_overview(
        DrawdownPlanStatusResult(0, [], [], []),
        profit_statuses=[{"params_json": "not-json", "rule_id": 19}],
    )
    assert "Price-Gain 19" not in text
    assert "Skipping malformed Price-Gain status rule_id=19" in caplog.text
    assert caplog.records[-1].name == "fund_alert_bot.commands"


def test_existing_plan_view_imports_stay_compatible():
    from fund_alert_bot import commands, plan_views

    for name, value in vars(plan_views).items():
        if getattr(value, "__module__", None) == plan_views.__name__:
            assert getattr(commands, name) is value


def test_views_load_without_shell_storage_or_telegram():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from types import SimpleNamespace; "
            "from fund_alert_bot.plan_views import format_plan_details; "
            "assert format_plan_details(SimpleNamespace("
            "statuses=[], no_data_skips=[], errors=[])) == ''; "
            "assert not {'fund_alert_bot.commands', 'fund_alert_bot.db', "
            "'fund_alert_bot.checks', 'telegram'} & sys.modules.keys()",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
