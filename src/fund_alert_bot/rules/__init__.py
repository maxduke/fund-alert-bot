"""Alert rule package."""

from fund_alert_bot.rules.drawdown import (
    build_drawdown_alerts,
    calculate_drawdown_from_high,
)
from fund_alert_bot.rules.profit import (
    build_profit_alert_key,
    build_profit_alerts,
    calculate_profit_rate,
)

__all__ = [
    "build_drawdown_alerts",
    "build_profit_alert_key",
    "build_profit_alerts",
    "calculate_drawdown_from_high",
    "calculate_profit_rate",
]
