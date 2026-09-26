import pytest

from fund_alert_bot.rules import dca, drawdown, profit
from fund_alert_bot.rules._params import (
    meets_threshold,
    read_params,
    read_rule_value,
)


def test_meets_threshold_absorbs_float_noise() -> None:
    assert meets_threshold(0.15 - 1e-13, 0.15)
    assert not meets_threshold(0.149, 0.15)


def test_read_rule_value_supports_mappings_rows_and_attributes() -> None:
    class Rule:
        symbol = "510300"

    assert read_rule_value({"symbol": "a"}, "symbol", None) == "a"
    assert read_rule_value(Rule(), "symbol", None) == "510300"
    assert read_rule_value(object(), "symbol", "fallback") == "fallback"


def test_read_params_accepts_json_and_mappings() -> None:
    assert read_params({"params_json": '{"a": 1}'}, subject="rule") == {"a": 1}
    assert read_params({"params": {"a": 1}}, subject="rule") == {"a": 1}
    assert read_params({}, subject="rule") == {}
    with pytest.raises(ValueError, match="^profit rule params_json must contain"):
        read_params({"params_json": "[]"}, subject="profit rule")


@pytest.mark.parametrize(
    ("module", "subject"),
    [(dca, "DCA rule"), (drawdown, "drawdown rule"), (profit, "profit rule")],
)
def test_each_rule_keeps_its_error_subject(module, subject) -> None:
    with pytest.raises(ValueError, match=f"^{subject} missing required param: x$"):
        module._read_required_param({}, "x")
    with pytest.raises(ValueError, match=f"^{subject} missing required field: x$"):
        module._read_required_rule_value({}, "x")
