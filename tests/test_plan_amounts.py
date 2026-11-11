from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from fund_alert_bot import checks
from fund_alert_bot.checks import (
    evaluate_drawdown_plan_prealerts,
    evaluate_drawdown_plan_rule,
    read_drawdown_plan_statuses,
)
from fund_alert_bot.command_args import parse_set_plan_amounts_args
from fund_alert_bot.commands import load_manual_add_selection
from fund_alert_bot.db import (
    add_rule,
    apply_manual_add_estimate,
    claim_notification_deliveries,
    complete_notification_delivery,
    ensure_notification_delivery_targets,
    get_active_drawdown_cycle,
    initialize_database,
    list_rules,
    open_connection,
    record_manual_addition,
    skip_drawdown_tiers_for_cycle,
    snooze_drawdown_tiers_for_date,
    upsert_fund_fee,
    upsert_position_snapshot,
)
from fund_alert_bot.i18n import localize_text, set_language
from fund_alert_bot.market_data.models import FundNav, RealtimeQuote
from fund_alert_bot.plan_amounts import (
    amount_revision,
    apply_plan_amounts,
    cycle_amount_config,
    preview_plan_amounts,
)
from fund_alert_bot.plan_views import format_plan_details, format_plan_overview
from fund_alert_bot.rules.drawdown_plan import parse_drawdown_plan_config


def _history(closes):
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(closes)),
            "close": closes,
            "source": "akshare_eastmoney",
        }
    )
    frame.attrs.update(
        symbol="510300",
        source="akshare_eastmoney",
        price_basis="qfq",
        frequency="daily",
    )
    return frame


def _config(connection):
    rule = list_rules(connection)[0]
    return parse_drawdown_plan_config(
        reference_symbol=rule["symbol"],
        asset_type=rule["asset_type"],
        params=json.loads(rule["params_json"]),
    )


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "bot.sqlite3"
    with open_connection(path) as connection:
        initialize_database(connection)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="Core",
            asset_type="cn_etf",
            params={
                "investment_fund_symbol": "000001",
                "tiers": [
                    {"drawdown": percent / 100, "amount": amount}
                    for percent, amount in (
                        (15, 5000),
                        (20, 10000),
                        (25, 15000),
                        (30, 20000),
                    )
                ],
            },
        )
        upsert_fund_fee(
            connection, fund_symbol="000001", fee_mode="rate", fee_value=0.001
        )
        upsert_position_snapshot(
            connection, fund_symbol="000001", units=1000, average_unit_cost=1.2
        )
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 74]),
            expected_date=date(2024, 1, 2),
        )
        selected = load_manual_add_selection(
            connection,
            plan_id=rule_id,
            action_date=date(2024, 1, 2),
            tier_keys=("0.15",),
        )
        record_manual_addition(
            connection,
            rule_id=rule_id,
            cycle_id=result.cycle_id,
            source_alert_event_id=selected.event_id,
            fund_symbol="000001",
            tiers=selected.tiers,
            action_at=datetime(2024, 1, 2, 6, tzinfo=UTC),
            create_estimate=True,
            cutoff_time="15:00",
            cutoff_choice="before",
            effective_date="2024-01-02",
        )
        skip_drawdown_tiers_for_cycle(
            connection, cycle_id=result.cycle_id, tier_keys=("0.25",)
        )
    return path, rule_id, result.cycle_id


def _preview(connection, rule_id, amounts="15:6000,20:11000,25:16000,30:21000"):
    command = parse_set_plan_amounts_args([str(rule_id), amounts])
    return preview_plan_amounts(connection, rule_id=rule_id, requested=command.tiers)


def _apply(connection, preview, *, update_pending=True, day=3):
    apply_plan_amounts(
        connection,
        preview,
        update_pending=update_pending,
        confirmed_unpurchased=update_pending,
        now=datetime(2024, 1, day, 6, tzinfo=UTC),
    )


def _rows(connection, table):
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


@pytest.mark.parametrize("update_pending", [True, False])
def test_mixed_states_survive_restart_and_drive_aggregated_reminders(
    plan, update_pending
):
    path, rule_id, cycle_id = plan
    protected = (
        "drawdown_cycles",
        "drawdown_tier_records",
        "manual_add_actions",
        "manual_add_estimates",
        "position_snapshots",
        "drawdown_tier_reminder_states",
    )
    with open_connection(path) as connection:
        before = {table: _rows(connection, table) for table in protected}
        preview = _preview(connection, rule_id)
        assert [change.state for change in preview.changes] == [
            "ADDED",
            "TRIGGERED_PENDING",
            "SKIPPED",
            "UNTRIGGERED",
        ]
        assert preview.pending_total == 10000
        assert preview.revised_pending_total == 11000
        _apply(connection, preview, update_pending=update_pending)
        assert {table: _rows(connection, table) for table in protected} == before
    with open_connection(path) as connection:
        initialize_database(connection)
        assert [tier.amount for tier in _config(connection).tiers] == [
            6000,
            11000,
            16000,
            21000,
        ]
        assert [
            tier.amount
            for tier in cycle_amount_config(
                connection, _config(connection), cycle_id
            ).tiers
        ] == [5000, 11000 if update_pending else 10000, 15000, 21000]
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 74, 69]),
            expected_date=date(2024, 1, 3),
        )
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM alert_events WHERE id = ?",
                (result.notification.event_id,),
            ).fetchone()[0]
        )
        assert [
            (tier["key"], tier["amount"]) for tier in payload["actionable_tiers"]
        ] == [("0.2", 11000 if update_pending else 10000), ("0.3", 21000)]
        assert payload["total_amount"] == (32000 if update_pending else 31000)
        assert payload["amount_revision"] == amount_revision(connection, rule_id)


def test_new_cycle_uses_new_amounts_without_buying_the_difference(plan):
    path, rule_id, old_cycle = plan
    with open_connection(path) as connection:
        _apply(connection, _preview(connection, rule_id))
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 74, 103]),
            expected_date=date(2024, 1, 3),
        )
        assert result.cycle_id != old_cycle
        assert result.notification is None
        result = evaluate_drawdown_plan_rule(
            connection,
            list_rules(connection)[0],
            _history([100, 74, 103, 68]),
            expected_date=date(2024, 1, 4),
        )
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM alert_events WHERE id = ?",
                (result.notification.event_id,),
            ).fetchone()[0]
        )
        assert [tier["amount"] for tier in payload["actionable_tiers"]] == [
            6000,
            11000,
            16000,
            21000,
        ]
        assert len(_rows(connection, "manual_add_actions")) == 1
        assert len(_rows(connection, "manual_add_estimates")) == 1


def test_historical_addition_uses_dated_amount_and_original_market_provenance(plan):
    path, rule_id, cycle_id = plan
    with open_connection(path) as connection:
        facts = _rows(connection, "drawdown_tier_records")
        _apply(connection, _preview(connection, rule_id, "20:11000"))
        for day, expected in ((2, 10000), (3, 11000)):
            selected = load_manual_add_selection(
                connection,
                plan_id=rule_id,
                action_date=date(2024, 1, day),
                tier_keys=("0.2",),
                historical=True,
            )
            assert selected.tiers[0].amount == expected
        record_manual_addition(
            connection,
            rule_id=rule_id,
            cycle_id=cycle_id,
            source_alert_event_id=selected.event_id,
            fund_symbol="000001",
            tiers=selected.tiers,
            action_at=datetime(2024, 1, 4, 6, tzinfo=UTC),
            action_date_override=selected.market_date,
            create_estimate=False,
            source_alert_event_ids=dict(selected.tier_source_event_ids),
            expected_amount_revision=selected.amount_revision,
        )
        assert _rows(connection, "drawdown_tier_records") == facts
        assert len(_rows(connection, "manual_add_estimates")) == 1
        assert [
            row["action_date"] for row in _rows(connection, "manual_add_actions")
        ] == ["2024-01-02", "2024-01-03"]


def test_old_reminder_and_confirmation_expire_even_if_amount_is_changed_back(plan):
    path, rule_id, cycle_id = plan
    with open_connection(path) as connection:
        old = load_manual_add_selection(
            connection,
            plan_id=rule_id,
            action_date=date(2024, 1, 2),
            tier_keys=("0.2",),
        )
        _apply(connection, _preview(connection, rule_id, "20:11000"), day=2)
        _apply(connection, _preview(connection, rule_id, "20:10000"), day=2)
        with pytest.raises(ValueError, match="fresh confirmation"):
            load_manual_add_selection(
                connection,
                plan_id=rule_id,
                action_date=date(2024, 1, 2),
                event_id=old.event_id,
                select_all=True,
            )
        with pytest.raises(sqlite3.IntegrityError, match="amounts changed"):
            record_manual_addition(
                connection,
                rule_id=rule_id,
                cycle_id=cycle_id,
                source_alert_event_id=old.event_id,
                fund_symbol="000001",
                tiers=old.tiers,
                action_at=datetime(2024, 1, 2, 7, tzinfo=UTC),
                create_estimate=False,
                expected_amount_revision=old.amount_revision,
            )
        fresh = load_manual_add_selection(
            connection,
            plan_id=rule_id,
            action_date=date(2024, 1, 2),
            tier_keys=("0.2",),
        )
        assert fresh.tiers[0].amount == 10000
        assert fresh.amount_revision > old.amount_revision


@pytest.mark.parametrize(
    "change", ["snooze", "skip", "purchase", "fee", "cycle", "edit"]
)
def test_intervening_state_change_rejects_entire_preview(plan, change):
    path, rule_id, cycle_id = plan
    with open_connection(path) as connection:
        preview = _preview(connection, rule_id)
        if change == "snooze":
            snooze_drawdown_tiers_for_date(
                connection,
                cycle_id=cycle_id,
                tier_keys=("0.2",),
                market_date="2024-01-03",
            )
        elif change == "skip":
            skip_drawdown_tiers_for_cycle(
                connection, cycle_id=cycle_id, tier_keys=("0.2",)
            )
        elif change == "purchase":
            selected = load_manual_add_selection(
                connection,
                plan_id=rule_id,
                action_date=date(2024, 1, 2),
                tier_keys=("0.2",),
            )
            record_manual_addition(
                connection,
                rule_id=rule_id,
                cycle_id=cycle_id,
                source_alert_event_id=selected.event_id,
                fund_symbol="000001",
                tiers=selected.tiers,
                action_at=datetime(2024, 1, 2, 7, tzinfo=UTC),
                create_estimate=False,
            )
        elif change == "fee":
            upsert_fund_fee(
                connection, fund_symbol="000001", fee_mode="rate", fee_value=0.002
            )
        elif change == "cycle":
            evaluate_drawdown_plan_rule(
                connection,
                list_rules(connection)[0],
                _history([100, 74, 103]),
                expected_date=date(2024, 1, 3),
            )
        else:
            _apply(connection, _preview(connection, rule_id, "30:22000"))
        before = (
            _rows(connection, "rules"),
            _rows(connection, "drawdown_plan_amount_changes"),
        )
        with pytest.raises(ValueError, match="state changed"):
            _apply(connection, preview)
        assert (
            _rows(connection, "rules"),
            _rows(connection, "drawdown_plan_amount_changes"),
        ) == before


def test_pending_change_requires_explicit_unpurchased_confirmation(plan):
    path, rule_id, _ = plan
    with open_connection(path) as connection:
        with pytest.raises(ValueError, match="not been purchased"):
            apply_plan_amounts(
                connection,
                _preview(connection, rule_id),
                update_pending=True,
                confirmed_unpurchased=False,
                now=datetime(2024, 1, 3, tzinfo=UTC),
            )
        assert amount_revision(connection, rule_id) == 0


def test_stale_evaluator_cannot_write_old_amounts(plan):
    path, rule_id, _ = plan
    with open_connection(path) as connection:
        stale_rule = list_rules(connection)[0]
        _apply(connection, _preview(connection, rule_id))
        with pytest.raises(sqlite3.IntegrityError, match="amounts changed"):
            evaluate_drawdown_plan_rule(
                connection,
                stale_rule,
                _history([100, 74, 69]),
                expected_date=date(2024, 1, 3),
            )
        assert (
            get_active_drawdown_cycle(connection, rule_id)["last_evaluated_date"]
            == "2024-01-02"
        )


def test_edit_revokes_old_delivery_claim_without_rewriting_event(plan):
    path, rule_id, _ = plan
    with open_connection(path) as connection:
        event = _rows(connection, "alert_events")[0]
        ensure_notification_delivery_targets(
            connection, event_ids=[event["id"]], targets=[("telegram:test", "telegram")]
        )
        claim = claim_notification_deliveries(connection, event_ids=[event["id"]])[0]
        _apply(connection, _preview(connection, rule_id))
        assert not complete_notification_delivery(
            connection,
            event_id=event["id"],
            target_key=claim.target_key,
            claim_token=claim.claim_token,
            result={"channel": "telegram", "success": False, "detail": "late"},
        )
        assert _rows(connection, "notification_deliveries")[0]["status"] == "cancelled"
        updated = _rows(connection, "alert_events")[0]
        assert updated["payload_json"] == event["payload_json"]
        assert updated["message"] == event["message"]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["1"],
        ["0", "15:100"],
        ["x", "15:100"],
        ["1", "15:0"],
        ["1", "15:nan"],
        ["1", "15:inf"],
        ["1", "20:100,15:100"],
        ["1", "15:100,15:200"],
    ],
)
def test_invalid_amount_edits_are_rejected(args):
    with pytest.raises(ValueError):
        parse_set_plan_amounts_args(args)


def test_unknown_threshold_and_fixed_fee_are_rejected_without_writes(plan):
    path, rule_id, _ = plan
    with open_connection(path) as connection:
        with pytest.raises(ValueError, match="existing tiers"):
            _preview(connection, rule_id, "16:100")
        upsert_fund_fee(
            connection, fund_symbol="000001", fee_mode="fixed", fee_value=100
        )
        with pytest.raises(ValueError, match="Fixed fee"):
            _preview(connection, rule_id, "20:100")
        assert amount_revision(connection, rule_id) == 0


def test_current_cycle_amounts_reach_status_and_realtime_prealerts(plan):
    path, rule_id, _ = plan

    class Provider:
        def get_history(self, *args, **kwargs):
            return _history([100, 74])

        def get_etf_realtime_quote(self, instrument):
            return RealtimeQuote(
                "510300",
                69,
                74,
                1000,
                69000,
                "eastmoney",
                datetime(2024, 1, 3, 6, 50, tzinfo=UTC),
            )

    with open_connection(path) as connection:
        _apply(connection, _preview(connection, rule_id), update_pending=False)
        statuses = read_drawdown_plan_statuses(
            connection,
            Provider(),
            end_date=date(2024, 1, 3),
            fund_nav_unavailable_reason="not fetched",
        )
        assert not statuses.errors
        assert statuses.statuses[0].config.tiers[1].amount == 10000
        overview = format_plan_overview(statuses)
        details = format_plan_details(statuses)
        assert "-20% / ¥10,000" in overview
        assert "Next-cycle amounts: -15% / ¥6,000, -20% / ¥11,000" in overview
        assert "Next-cycle amount: ¥11,000" in details
        set_language("zh-CN")
        try:
            assert "下周期金额：" in localize_text(overview)
        finally:
            set_language("en")
        result = evaluate_drawdown_plan_prealerts(
            connection,
            Provider(),
            market_date=date(2024, 1, 3),
            confirmed_date=date(2024, 1, 2),
        )
        assert not result.errors
        assert len(result.notifications) == 1
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM alert_events WHERE id = ?",
                (result.notifications[0].event_id,),
            ).fetchone()[0]
        )
        assert [item["amount"] for item in payload["actionable_tiers"]] == [
            10000,
            21000,
        ]
        assert payload["total_amount"] == 31000


def test_prealert_rejects_amount_edit_between_cycle_read_and_reservation(
    plan, monkeypatch
):
    path, rule_id, _ = plan

    class Provider:
        def get_history(self, *args, **kwargs):
            return _history([100, 74])

        def get_etf_realtime_quote(self, instrument):
            return RealtimeQuote(
                "510300",
                69,
                74,
                1000,
                69000,
                "eastmoney",
                datetime(2024, 1, 3, 6, 50, tzinfo=UTC),
            )

    with open_connection(path) as connection:
        _apply(
            connection, _preview(connection, rule_id, "20:11000"), update_pending=False
        )
        original = checks.cycle_amount_config

        def edit_after_read(conn, config, cycle_id):
            old = original(conn, config, cycle_id)
            _apply(conn, _preview(conn, rule_id, "20:11000"))
            return old

        monkeypatch.setattr(checks, "cycle_amount_config", edit_after_read)
        result = evaluate_drawdown_plan_prealerts(
            connection,
            Provider(),
            market_date=date(2024, 1, 3),
            confirmed_date=date(2024, 1, 2),
        )
        assert not result.notifications
        assert len(_rows(connection, "alert_events")) == 1


def test_failed_audit_insert_rolls_back_config_and_delivery_changes(plan):
    path, rule_id, _ = plan
    with open_connection(path) as connection:
        preview = _preview(connection, rule_id)
        before = _rows(connection, "rules"), _rows(connection, "alert_events")
        connection.execute("""CREATE TRIGGER reject_amount_edit BEFORE INSERT
            ON drawdown_plan_amount_changes
            BEGIN SELECT RAISE(ABORT, 'disk write failed'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="disk write failed"):
            _apply(connection, preview)
        assert not connection.in_transaction
        assert amount_revision(connection, rule_id) == 0
        assert (_rows(connection, "rules"), _rows(connection, "alert_events")) == before


def test_changed_purchased_tier_settles_with_original_gross_and_fee(plan):
    path, rule_id, _ = plan
    with open_connection(path) as connection:
        estimate_id = _rows(connection, "manual_add_estimates")[0]["id"]
        _apply(connection, _preview(connection, rule_id, "15:8000"))
        applied = apply_manual_add_estimate(
            connection,
            estimate_id=estimate_id,
            nav=FundNav("000001", date(2024, 1, 2), 1.25, "akshare_eastmoney"),
        )
        assert applied is not None
        estimate = _rows(connection, "manual_add_estimates")[0]
        assert estimate["gross_amount"] == 5000
        assert estimate["added_units"] == pytest.approx(5000 / 1.001 / 1.25)
        assert len(_rows(connection, "manual_add_actions")) == 1


def test_amounts_can_change_before_first_cycle_and_an_existing_pending_override(plan):
    path, rule_id, cycle_id = plan
    with open_connection(path) as connection:
        # A deferred amount can still be changed explicitly later in this cycle.
        _apply(
            connection, _preview(connection, rule_id, "20:11000"), update_pending=False
        )
        pending = _preview(connection, rule_id, "20:11000")
        assert pending.changes_pending
        _apply(connection, pending)
        assert (
            cycle_amount_config(connection, _config(connection), cycle_id)
            .tiers[1]
            .amount
            == 11000
        )
        empty_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="159915",
            name="New",
            asset_type="cn_etf",
            params={
                "investment_fund_symbol": "110026",
                "tiers": [{"drawdown": 0.15, "amount": 1000}],
            },
        )
        preview = _preview(connection, empty_id, "15:1200")
        assert preview.cycle_id is None
        _apply(connection, preview)
        fresh_rule = next(
            row for row in list_rules(connection) if row["id"] == empty_id
        )
        assert json.loads(fresh_rule["params_json"])["tiers"][0]["amount"] == 1200
        assert get_active_drawdown_cycle(connection, empty_id) is None
