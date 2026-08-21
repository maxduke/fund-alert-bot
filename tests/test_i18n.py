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
        assert localize_text("✅ Read-only Drawdown Add Plans checked: 4.") == (
            "✅ 已检查回撤加仓计划（只读）：4."
        )
    finally:
        set_language("en")

    assert localize_text("仅记录 -10% → ¥5000") == "Record only -10% → ¥5000"
    assert localize_text("当前精确持仓已包含，仅补记档位") == (
        "Current exact position already includes it; record tiers only"
    )


def test_chinese_localizes_readable_rule_list_fields() -> None:
    set_language("zh-CN")
    try:
        assert localize_text(
            "• #3 A500\n"
            "Type: drawdown_plan\n"
            "Status: enabled\n"
            "Reference ETF: 510300\n"
            "Investment fund: 000001\n"
            "Tiers: -15%:¥5,000"
        ) == (
            "• #3 A500\n"
            "类型： drawdown_plan\n"
            "状态：已启用\n"
            "参考 ETF： 510300\n"
            "实际投资基金： 000001\n"
            "档位： -15%:¥5,000"
        )
    finally:
        set_language("en")


def test_chinese_localizes_new_confirmation_workflows() -> None:
    set_language("zh-CN")
    try:
        assert localize_text(
            "Action: soft-disable\n"
            "Confirm only if this deduction failed or was not executed. "
            "This occurrence will not update the estimated position.\n"
            "Disabled rule id=3 (A500)."
        ) == (
            "操作：停用并保留历史\n"
            "仅在本期扣款失败或未执行时确认。 本期不会更新估算持仓。\n"
            "已停用规则 id=3 (A500)."
        )
    finally:
        set_language("en")


def test_localizes_dynamic_success_messages_without_changing_names() -> None:
    set_language("zh-CN")
    try:
        assert (
            localize_text(
                "Updated DCA rule id=12 Save future amount to Growth "
                "future amount to ¥500."
                " Existing occurrences are unchanged."
            )
            == "已更新定投规则 id=12 Save future amount to Growth "
            "未来金额更新为 ¥500。"
            "已有期次保持不变。"
        )
        assert localize_text(
            "Saved Drawdown Add Plan id=3: ETF 588000 → fund 011608. "
            "The first scheduled confirmed-close evaluation will initialize "
            "its cycle. No order has been placed."
        ) == (
            "已保存回撤加仓计划 id=3：ETF 588000 → 基金 011608。"
            "首次定时收盘确认将初始化其周期。未执行任何交易。"
        )
    finally:
        set_language("en")


def test_english_localizes_dynamic_cutoff_buttons() -> None:
    set_language("en")

    assert localize_text("15:00前已提交 — 当日净值") == (
        "15:00 submitted before cutoff — same-day NAV"
    )
    assert localize_text("15:00后才提交 — 下一开放日") == (
        "15:00 submitted after cutoff — next open-day NAV"
    )


def test_chinese_localizes_test_notification_title() -> None:
    set_language("zh-CN")
    try:
        assert localize_text("fund-alert-bot test") == "fund-alert-bot 测试"
        assert localize_text("Sent test notification to 3 channel(s).") == (
            "测试通知已发送至 3 个渠道。"
        )
        assert localize_text("Sent test notification to 1 of 3 channel(s).") == (
            "测试通知已发送至 1/3 个渠道。"
        )
    finally:
        set_language("en")


def test_chinese_localizes_proxy_balance_warning() -> None:
    set_language("zh-CN")
    try:
        assert localize_text(
            "Paid proxy not enabled\n"
            "The paid proxy was not enabled because its balance is insufficient "
            "or could not be verified.\n"
            "Direct data sources will be used.\n"
            "Recharge or fix the proxy token, then restart the bot."
        ) == (
            "付费代理未启用\n"
            "付费代理未启用，因为积分不足或无法验证余额。\n"
            "将使用直连数据源。\n"
            "请充值或修复代理 Token，然后重启 Bot。"
        )
    finally:
        set_language("en")


def test_chinese_localizes_dynamic_rule_not_found_suffix() -> None:
    set_language("zh-CN")
    try:
        assert localize_text("Rule id=4 was not found") == "规则 id=4 未找到"
    finally:
        set_language("en")


def test_chinese_localizes_known_label_suffixes_and_active_decorations() -> None:
    set_language("zh-CN")
    try:
        assert localize_text("Lookback: 365 calendar days") == "回看周期： 365 个日历日"
        assert (
            localize_text("Position value: unavailable (unit NAV could not be fetched)")
            == "持仓市值： 不可用（无法获取单位净值）"
        )
        assert localize_text("📊 Investment Plans") == "📊 投资计划"
        assert localize_text("🔔 New alerts: 1.") == "🔔 新提醒： 1."
        assert localize_text("🧪 Test notification") == "🧪 测试通知"
    finally:
        set_language("en")


def test_chinese_localizes_complete_drawdown_and_profit_alerts() -> None:
    set_language("zh-CN")
    try:
        drawdown = localize_text(
            "\n".join(
                (
                    "📉 Drawdown reminder",
                    "• Symbol: 399006",
                    "• Name: 创业板指",
                    "• Asset type: cn_index",
                    "• Lookback: 365 days",
                    "• Drawdown: 10.0%",
                    "• Triggered threshold: 10.0%",
                    "• Peak: 100 on 2024-01-01",
                    "• Latest: 90 on 2024-01-02",
                    "Reminder: this is not automatic trading and no orders will "
                    "be placed.",
                )
            )
        )
        assert drawdown == "\n".join(
            (
                "📉 回撤提醒",
                "• 代码： 399006",
                "• 名称： 创业板指",
                "• 资产类型： cn_index",
                "• 回看周期： 365 天",
                "• 回撤： 10.0%",
                "• 触发阈值： 10.0%",
                "• 高点： 100，日期：2024-01-01",
                "• 最新价： 90，日期：2024-01-02",
                "提醒：这不是自动交易，不会执行任何订单。",
            )
        )

        profit = localize_text(
            "💰 Price-Gain reminder\n• Latest price: 2.4\n• Profit rate: 29.7%"
        )
        assert profit == "💰 涨幅提醒\n• 最新价： 2.4\n• 涨幅： 29.7%"
    finally:
        set_language("en")


def test_chinese_localizes_merged_fixed_dca_reminder() -> None:
    set_language("zh-CN")
    try:
        assert localize_text(
            "\n".join(
                (
                    "💰 Fixed DCA reminders",
                    "Scheduled date: 2026-08-13",
                    "• Fund: 000001 / A500",
                    "• Gross amount: 2000 RMB",
                    "• Holiday policy: next",
                    "• Waiting for the next confirmed open day before "
                    "estimating units.",
                    "Total planned amount: 2000 RMB",
                )
            )
        ) == "\n".join(
            (
                "💰 固定定投提醒",
                "计划日期： 2026-08-13",
                "• 基金： 000001 / A500",
                "• 计划总额： 2000 元",
                "• 节假日策略：顺延至下一开放日",
                "• 等待下一个确认开市日后再估算份额。",
                "本次计划合计： 2000 元",
            )
        )
        assert localize_text("⚠️ Deduction failed — A500") == ("⚠️ 扣款失败 — A500")
    finally:
        set_language("en")


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
