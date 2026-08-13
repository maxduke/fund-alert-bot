"""Alert rule package."""

from fund_alert_bot.rules.drawdown import (
    build_drawdown_alerts,
    calculate_drawdown_from_high,
)
from fund_alert_bot.rules.drawdown_plan import (
    ActiveDrawdownCycle,
    DrawdownPlanConfig,
    DrawdownPlanEvaluation,
    DrawdownTier,
    build_drawdown_plan_alert,
    calculate_sma,
    calculate_sma_distance,
    calculate_sma_slope,
    evaluate_drawdown_plan,
    parse_drawdown_plan_config,
    required_history_start,
    validate_confirmed_plan_history,
    validate_realtime_quote,
)
from fund_alert_bot.rules.profit import (
    build_profit_alert_key,
    build_profit_alerts,
    calculate_profit_rate,
)

__all__ = [
    "ActiveDrawdownCycle",
    "DrawdownPlanConfig",
    "DrawdownPlanEvaluation",
    "DrawdownTier",
    "build_drawdown_alerts",
    "build_drawdown_plan_alert",
    "build_profit_alert_key",
    "build_profit_alerts",
    "calculate_drawdown_from_high",
    "calculate_profit_rate",
    "calculate_sma",
    "calculate_sma_distance",
    "calculate_sma_slope",
    "evaluate_drawdown_plan",
    "parse_drawdown_plan_config",
    "required_history_start",
    "validate_confirmed_plan_history",
    "validate_realtime_quote",
]
