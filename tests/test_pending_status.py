from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from fund_alert_bot.db import (
    add_enhanced_dca_rule,
    connect,
    create_scheduled_dca_occurrence,
    ensure_notification_delivery_targets,
    init_db,
    reserve_alert_event,
    upsert_fund_nav,
    upsert_position_snapshot,
)
from fund_alert_bot.i18n import get_language, set_language
from fund_alert_bot.pending_status import format_pending_work


@pytest.fixture
def database():
    connection = connect(":memory:")
    init_db(connection)
    rule = add_enhanced_dca_rule(
        connection,
        fund_symbol="110026",
        name="Private account name",
        weekday="MON",
        amount=1000,
        fee_mode="rate",
        fee_value=0,
        holiday_policy="next",
        created_at="1999-01-01T00:00:00+00:00",
    )
    create_scheduled_dca_occurrence(
        connection,
        rule_id=rule,
        fund_symbol="110026",
        due_date="2000-01-01",
        gross_amount=1000,
        holiday_policy="next",
        effective_date="2000-01-01",
        skipped=False,
    )
    event = reserve_alert_event(
        connection, rule_id=rule, alert_key="private", title="secret", message="secret"
    )
    connection.execute(
        """
        INSERT INTO drawdown_cycles (
            id, rule_id, initial_peak_date, peak_date, initial_peak_price,
            peak_price, last_evaluated_date, created_at, updated_at
        ) VALUES (1, ?, '2000-01-01', '2000-01-01', 1, 1,
                  '2000-01-01', '2000-01-01', '2000-01-01')
        """,
        (rule,),
    )
    connection.execute(
        """
        INSERT INTO manual_add_estimates (
            rule_id, cycle_id, source_alert_event_id, fund_symbol, tier_keys_json,
            gross_amount, fee_mode, fee_value, action_at, action_date,
            cutoff_time, cutoff_choice, effective_date, created_at, updated_at
        ) VALUES (?, 1, ?, '110026', '["0.15"]', 1000, 'rate', 0,
                  '2000-01-01', '2000-01-01', '15:00', 'before', '2000-01-01',
                  '2000-01-01T00:00:00+00:00', '2000-01-01')
        """,
        (rule, event),
    )
    connection.execute(
        "UPDATE scheduled_dca_occurrences SET created_at='2000-01-01T00:00:00+00:00'"
    )
    connection.commit()
    language = get_language()
    set_language("en")
    try:
        yield connection
    finally:
        set_language(language)
        connection.close()


def _text(connection, today=date(2000, 1, 3)):
    return "\n".join(
        format_pending_work(connection, zone=ZoneInfo("Asia/Shanghai"), today=today)
    )


def _position(connection):
    upsert_position_snapshot(
        connection, fund_symbol="110026", units=1000, average_unit_cost=1
    )


@pytest.mark.parametrize(
    ("case", "english", "chinese"),
    [
        ("missing_position", "Initial position is missing", "缺少初始持仓"),
        (
            "position_sync",
            "Position reconciliation is required",
            "持仓已标记为需要对账",
        ),
        (
            "settings_sync",
            "Position reconciliation is required",
            "持仓已标记为需要对账",
        ),
        (
            "missing_nav",
            "Exact-date NAV unavailable locally",
            "本地没有可用的准确日期净值",
        ),
        (
            "stale_nav",
            "Exact-date NAV unavailable locally",
            "本地没有可用的准确日期净值",
        ),
        (
            "wrong_source",
            "Exact-date NAV unavailable locally",
            "本地没有可用的准确日期净值",
        ),
        ("ready", "Reason unknown from local records", "无法从本地记录确定待处理原因"),
        (
            "missing_settings",
            "Reason unknown from local records",
            "无法从本地记录确定待处理原因",
        ),
    ],
)
def test_local_evidence_for_both_estimate_types(database, case, english, chinese):
    if case != "missing_position":
        _position(database)
    if case in {"position_sync", "settings_sync"}:
        table = "position_snapshots" if case == "position_sync" else "fund_settings"
        database.execute(
            f"UPDATE {table} SET position_sync_required_since='2000-01-01'"
        )
    if case in {"stale_nav", "wrong_source", "ready", "missing_settings"}:
        upsert_fund_nav(
            database,
            fund_symbol="110026",
            nav_date=date(1999, 12, 31) if case == "stale_nav" else date(2000, 1, 1),
            unit_nav=1.2,
            source="wrong" if case == "wrong_source" else "akshare_eastmoney",
        )
    if case == "missing_settings":
        # Both processors use the saved fee; absent current settings are not a blocker.
        database.execute("DELETE FROM fund_settings")
    database.commit()
    before = list(database.iterdump())
    database.execute("PRAGMA query_only=ON")
    for language, expected in [("en", english), ("zh-CN", chinese)]:
        set_language(language)
        text = _text(database)
        assert text.count(expected) == 2
        assert "110026" in text and "2000-01-01" in text
        assert "2000-01-01 08:00" in text
        assert "Private account name" not in text and "secret" not in text
        if case in {"missing_position", "position_sync", "settings_sync"}:
            assert text.count("/sync_position 110026") == 2
        else:
            assert "/sync_position" not in text
    assert list(database.iterdump()) == before


def test_pre_creation_uses_local_timezone_and_gives_conditional_skip(database):
    database.execute("UPDATE rules SET created_at='2000-01-01T17:00:00+00:00'")
    text = _text(database)
    assert text.count("Predates rule creation") == 1  # DCA only
    assert "/dca_skip 1 2000-01-01" in text
    assert "If not executed:" in text
    assert "Rule 1 / 110026" in text


@pytest.mark.parametrize(
    ("effective", "expected"),
    [
        (None, "Effective trading date not yet recorded"),
        ("2000-01-03", "Effective date has not completed"),
        ("2000-01-04", "Effective date has not completed"),
        ("corrupt", "Reason unknown from local records"),
    ],
)
def test_unresolved_future_and_invalid_effective_dates(database, effective, expected):
    database.execute(
        "UPDATE scheduled_dca_occurrences SET effective_date=?", (effective,)
    )
    assert expected in _text(database)
    assert "corrupt" not in _text(database)


def test_invalid_creation_is_unknown_not_a_sync_instruction(database):
    database.execute("UPDATE rules SET created_at='invalid'")
    text = _text(database).split("Oldest pending additions:")[0]
    assert "Reason unknown from local records" in text
    assert "/sync_position" not in text


def test_delivery_details_hide_destinations_and_raw_failures(database):
    ensure_notification_delivery_targets(
        database,
        event_ids=[1],
        targets=[("webhook:https://secret.example/token", "webhook")],
    )
    database.execute(
        """
        UPDATE notification_deliveries SET status='failed',
            created_at='2000-01-01T02:00:00+02:00',
            result_json='{"error":"secret-token-raw-error"}'
        """
    )
    text = _text(database)
    assert "Event 1 / Rule 1: Delivery failed" in text
    assert "2000-01-01 08:00" in text
    assert "secret" not in text and "webhook" not in text
    for state, expected in [
        ("pending", "Awaiting delivery"),
        ("sending", "Delivery in flight"),
    ]:
        database.execute("UPDATE notification_deliveries SET status=?", (state,))
        assert expected in _text(database)
    database.execute("UPDATE notification_deliveries SET status='sent'")
    assert "Oldest unfinished deliveries:" not in _text(database)


def test_oldest_order_limit_and_completed_items(database):
    for index in range(2, 22):
        create_scheduled_dca_occurrence(
            database,
            rule_id=1,
            fund_symbol="110026",
            due_date=f"2000-01-{index:02}",
            gross_amount=1000,
            holiday_policy="next",
            effective_date=None,
            skipped=False,
        )
    # Offset timestamps must sort by instant, not by the text representation.
    database.execute(
        "UPDATE scheduled_dca_occurrences "
        "SET created_at='2000-01-01T01:00:00+02:00' WHERE id=2"
    )
    database.execute("UPDATE scheduled_dca_occurrences SET status='skipped' WHERE id=3")
    text = _text(database)
    dca = text.split("Oldest pending additions:")[0]
    assert dca.index("#2 / Rule") < dca.index("#1 / Rule")
    assert dca.count("• #") == 3
    assert "#3 / Rule" not in dca
    assert "Additional pending items omitted." in dca
    assert "Delivery targets not yet assigned" in text
    assert len(text) < 3000
