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
