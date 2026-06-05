from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from fund_alert_bot.checks import DCA_RULE_TYPE, evaluate_dca_rules
from fund_alert_bot.db import add_rule, connect, init_db
from fund_alert_bot.rules.dca import (
    build_dca_alert_key,
    build_dca_reminder_alert,
    normalize_weekday,
)


@pytest.mark.parametrize(
    ("raw_weekday", "expected"),
    [
        ("周一", "MON"),
        ("周二", "TUE"),
        ("周三", "WED"),
        ("周四", "THU"),
        ("周五", "FRI"),
        ("周六", "SAT"),
        ("周日", "SUN"),
    ],
)
def test_parse_chinese_weekdays(raw_weekday: str, expected: str) -> None:
    assert normalize_weekday(raw_weekday) == expected


@pytest.mark.parametrize(
    ("raw_weekday", "expected"),
    [
        ("Monday", "MON"),
        ("Tuesday", "TUE"),
        ("Wednesday", "WED"),
        ("Thursday", "THU"),
        ("Friday", "FRI"),
        ("Saturday", "SAT"),
        ("Sunday", "SUN"),
    ],
)
def test_parse_english_weekdays(raw_weekday: str, expected: str) -> None:
    assert normalize_weekday(raw_weekday) == expected


def test_dca_rule_builds_due_alert() -> None:
    alert = build_dca_reminder_alert(
        _rule(weekday="THU", amount=1000),
        date(2024, 1, 4),
        _never_seen,
    )

    assert alert is not None
    assert alert["alert_key"] == "dca:7:2024-01-04"
    assert alert["title"] == "DCA reminder"
    assert alert["message"] == (
        "今天是 创业板 定投日，计划定投 1000 元。\n"
        "提醒：这是纪律提醒，不会自动交易。"
    )
    assert alert["payload"] == {
        "rule_id": 7,
        "name": "创业板",
        "weekday": "THU",
        "amount": 1000,
        "due_date": "2024-01-04",
    }


def test_dca_rule_skips_when_not_due_today() -> None:
    alert = build_dca_reminder_alert(
        _rule(weekday="THU", amount=1000),
        date(2024, 1, 5),
        _never_seen,
    )

    assert alert is None


def test_dca_rule_skips_existing_daily_alert_key() -> None:
    existing_keys = {"dca:7:2024-01-04"}

    alert = build_dca_reminder_alert(
        _rule(weekday="THU", amount=1000),
        date(2024, 1, 4),
        existing_keys.__contains__,
    )

    assert alert is None


def test_dca_alert_key_uses_rule_id_and_date() -> None:
    assert build_dca_alert_key(rule_id=12, due_date=date(2024, 1, 4)) == (
        "dca:12:2024-01-04"
    )


def test_evaluate_dca_rules_sends_once_per_day() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        rule_id = add_rule(
            connection,
            type=DCA_RULE_TYPE,
            symbol="创业板",
            name="创业板",
            asset_type="dca",
            params={"weekday": "THU", "amount": 1000},
        )

        first_result = evaluate_dca_rules(connection, today=date(2024, 1, 4))
        second_result = evaluate_dca_rules(connection, today=date(2024, 1, 4))
        event_rows = connection.execute(
            """
            SELECT alert_key, message
            FROM alert_events
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()

    assert len(first_result.notifications) == 1
    assert len(second_result.notifications) == 0
    assert [row["alert_key"] for row in event_rows] == [
        f"dca:{rule_id}:2024-01-04"
    ]
    assert event_rows[0]["message"] == (
        "今天是 创业板 定投日，计划定投 1000 元。\n"
        "提醒：这是纪律提醒，不会自动交易。"
    )


def _rule(*, weekday: str, amount: int | float) -> dict[str, Any]:
    return {
        "id": 7,
        "name": "创业板",
        "params": {
            "weekday": weekday,
            "amount": amount,
        },
    }


def _never_seen(_alert_key: str) -> bool:
    return False
