import asyncio

from fund_alert_bot.i18n import localize_actions, localize_text, set_language
from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.service import NotificationService


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> NotificationResult:
        self.messages.append(message)
        return NotificationResult(channel=self.name, success=True, detail="sent")


def test_chinese_localizes_text_buttons_and_all_notification_channels() -> None:
    set_language("zh-CN")
    try:
        assert localize_text("📉 Buy-plan reminder — A500") == (
            "📉 加仓计划提醒 — A500"
        )
        assert localize_actions(((("Cancel", "keep:callback"),),)) == (
            (("取消", "keep:callback"),),
        )

        channel = RecordingChannel()
        service = NotificationService([channel])
        asyncio.run(
            service.send_alert(
                title="Price-Gain reminder",
                body="This is a price-gain reminder only. No trade has been placed.",
                telegram_actions=((("Confirm zero position", "close:1"),),),
            )
        )

        assert channel.messages == [
            NotificationMessage(
                title="涨幅提醒",
                body="这只是涨幅提醒。 未执行任何交易。",
                telegram_actions=((("确认持仓为零", "close:1"),),),
            )
        ]
    finally:
        set_language("en")


def test_english_normalizes_existing_chinese_user_text() -> None:
    set_language("en")

    assert localize_text("• 标的：创业板\n提醒：这是纪律提醒，不会自动交易。") == (
        "• Asset: 创业板\n"
        "Reminder: this is a discipline reminder; no automatic trade occurs."
    )


def test_localization_does_not_modify_user_provided_english_names() -> None:
    set_language("zh-CN")
    try:
        assert localize_text("• Name: Updated Income") == "• 名称： Updated Income"
        assert localize_text("Fund: Falling Star") == "基金： Falling Star"
        assert localize_text("• Name: No action") == "• 名称： No action"
    finally:
        set_language("en")


def test_chinese_translates_dynamic_profit_setup_button() -> None:
    set_language("zh-CN")
    try:
        assert localize_text("Set gain thresholds — 110026") == (
            "设置涨幅阈值 — 110026"
        )
    finally:
        set_language("en")


def test_localizes_help_and_dynamic_status_structures() -> None:
    set_language("zh-CN")
    try:
        assert localize_text("/start - Start the bot") == "/start - 启动 Bot"
        assert localize_text("✅ Checked 1 drawdown_from_high rule(s).") == (
            "✅ 已检查 1 drawdown_from_high rule(s)."
        )
    finally:
        set_language("en")

    assert localize_text("仅记录 -10% → ¥5000") == "Record only -10% → ¥5000"


def test_chinese_translates_every_dca_skip_outcome() -> None:
    set_language("zh-CN")
    try:
        assert "已经应用" in localize_text(
            "This estimate was already applied. Use /sync_position to correct "
            "the platform position; no units were subtracted."
        )
        assert "持仓同步完成对账" in localize_text(
            "This occurrence was already reconciled by Position Sync."
        )
    finally:
        set_language("en")
