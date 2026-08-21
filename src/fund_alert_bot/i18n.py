# ruff: noqa: E501
"""Small process-wide localization for user-facing messages."""

from __future__ import annotations

SUPPORTED_LANGUAGES = frozenset({"en", "zh-CN"})
_language = "en"

# The bot is a single-user, single-language process. Phrase replacement keeps
# rule evaluation and persisted alert identities independent from presentation.
_EN_TO_ZH = {
    "fund-alert-bot is running. Use /help to see available commands.": "fund-alert-bot 正在运行。使用 /help 查看可用命令。",
    "Available commands:": "可用命令：",
    "Start the bot": "启动 Bot",
    "Show available commands": "显示可用命令",
    "Add a drawdown reminder": "添加回撤提醒",
    "Add a price-gain reminder": "添加涨幅提醒",
    "Add a recurring DCA reminder": "添加周期定投提醒",
    "Change future DCA amounts": "修改未来定投金额",
    "Mark a DCA deduction as skipped": "标记定投扣款未执行",
    "Change a fund subscription fee": "修改基金申购手续费",
    "Change a fund subscription cutoff": "修改基金申购截止时间",
    "Sync a feeder-fund position": "同步联接基金持仓",
    "Add a drawdown buy plan": "添加回撤加仓计划",
    "Record a completed addition": "记录已完成的加仓",
    "Test enabled notification channels": "测试已启用的通知渠道",
    "Reminder only": "仅提醒",
    "Fixed fund DCA estimate": "固定基金定投估算",
    "Deduction failed/not executed": "扣款失败或未执行",
    "Deduction failed/not executed; this occurrence is skipped.": "扣款失败或未执行；本期已跳过。",
    "Future occurrences only": "仅影响未来新建的期次",
    "Record an addition you made": "记录你已实际提交的加仓",
    "Show investment-plan status": "显示投资计划状态",
    "List configured rules": "列出已配置规则",
    "List configured rules and IDs": "列出已配置规则及其 ID",
    "Remove a configured rule": "删除已配置规则",
    "Run a manual check": "手动执行检查",
    "Run checks and send any triggered alerts": "立即检查并发送触发提醒",
    "Send a test notification to all enabled channels": "向所有已启用渠道发送测试通知",
    "Usage:": "用法：",
    "or:": "或：",
    "You are not allowed to use this bot.": "你无权使用此 Bot。",
    "No rules configured": "尚未配置规则",
    "No investment plans or positions configured.": "尚未配置投资计划或持仓。",
    "No enabled notification channels.": "没有已启用的通知渠道。",
    "Paid proxy not enabled": "付费代理未启用",
    (
        "The paid proxy was not enabled because its balance is insufficient or "
        "could not be verified."
    ): "付费代理未启用，因为积分不足或无法验证余额。",
    "Direct data sources will be used.": "将使用直连数据源。",
    "Recharge or fix the proxy token, then restart the bot.": (
        "请充值或修复代理 Token，然后重启 Bot。"
    ),
    "Configured rules:": "已配置规则：",
    "Check summary": "检查摘要",
    "Checked": "已检查",
    "Checked ": "已检查 ",
    "Read-only Drawdown Add Plans checked: ": "已检查回撤加仓计划（只读）：",
    "New alerts:": "新提醒：",
    "No alerts triggered.": "没有触发提醒。",
    "Duplicate alerts skipped:": "已跳过重复提醒：",
    "No-data skips:": "因无数据跳过：",
    "Errors:": "错误：",
    "Rule:": "规则：",
    "Type:": "类型：",
    "Status:": "状态：",
    "Action:": "操作：",
    "Current drawdowns": "当前回撤",
    "from high": "相对高点",
    "latest": "最新价",
    "Position synced for fund": "已同步基金持仓",
    "Units:": "份额：",
    "Average unit cost:": "平均单位成本：",
    "Accuracy: exact (sales-platform sync)": "准确性：精确（销售平台同步）",
    "Last sync:": "最近同步：",
    "Applied estimates since sync:": "同步后已应用估算：",
    "Position value:": "持仓市值：",
    "Position value": "持仓市值",
    "unavailable (unit NAV could not be fetched)": "不可用（无法获取单位净值）",
    "Position: unavailable": "持仓：不可用",
    "Position: closed": "持仓：已清仓",
    "Position: not synced": "持仓：尚未同步",
    "Position Sync required": "需要同步持仓",
    "Unit NAV was not requested.": "未请求单位净值。",
    "unit NAV could not be fetched": "无法获取单位净值",
    "Latest unit NAV:": "最新单位净值：",
    (
        "Reminder: sync again after any redemption, distribution, unrecorded "
        "purchase, fee mismatch, or visible platform difference."
    ): "提醒：发生赎回、分红、未记录购买、手续费不符或平台数据差异后，请重新同步持仓。",
    "The change applies only to future estimates.": "本次修改只影响未来估算。",
    "Confirm Drawdown Add Plan": "确认回撤加仓计划",
    "Reference ETF:": "参考 ETF：",
    "Investment feeder fund:": "实际持有的联接基金：",
    "Investment fund:": "实际投资基金：",
    "Display name:": "显示名称：",
    "Lookback:": "回看周期：",
    "calendar days": "个日历日",
    "days": "天",
    "RMB": "元",
    "Tiers (incremental):": "档位（增量金额）：",
    "Tiers:": "档位：",
    "Weekday:": "星期：",
    "Maximum one-cycle total:": "单周期最大总额：",
    "MA250 / 20-session slope: context only": "MA250 / 20 个交易日斜率：仅作背景信息",
    "Plan readiness:": "计划准备状态：",
    "Missing setup:": "缺少设置：",
    "Reference ETF data: verified as qfq daily history": (
        "参考 ETF 数据：已验证前复权日线"
    ),
    "Data date:": "数据日期：",
    "Current drawdown preview:": "当前回撤预览：",
    "Currently reached tiers:": "当前已到达档位：",
    "Feeder-fund NAV: verified": "联接基金净值：已验证",
    (
        "Confirm only if these codes are the intended ETF/feeder pair and the "
        "fund follows the domestic A-share valuation calendar."
    ): "仅在两个代码确为目标 ETF/联接基金配对，且基金采用境内 A 股估值日历时确认。",
    "This saves reminder rules only. It never places an order.": (
        "这里只保存提醒规则，绝不会下单。"
    ),
    "Investment Plans": "投资计划",
    "Thresholds:": "阈值：",
    "Holiday policy:": "节假日策略：",
    "Latest occurrence: none yet": "最近期次：暂无",
    "Estimated units added:": "估算新增份额：",
    "Drawdown:": "回撤：",
    "Next open tier: all tiers already reminded": "下一档：所有档位均已提醒",
    "Next open tier:": "下一档：",
    "Reached, awaiting official close confirmation:": "已达到，等待收盘确认：",
    "Triggered, still pending:": "已触发，仍待处理：",
    "Skipped for this cycle:": "本周期已跳过：",
    "Drawdown Add Plan status (read-only)": "回撤加仓计划状态（只读）",
    "Current:": "当前价：",
    "Peak:": "高点：",
    "Readiness:": "准备状态：",
    "Next level: all tiers already reminded": "下一档：所有档位均已提醒",
    "Next level:": "下一档：",
    "Distance to next level:": "距下一档：",
    "UNTRIGGERED": "未触发",
    "TRIGGERED_PENDING": "已触发待处理",
    "ADDED (user-confirmed)": "已加仓（用户确认）",
    "SKIPPED (this cycle)": "已跳过（本周期）",
    "; deferred today": "；今天已顺延提醒",
    "percentage points": "个百分点",
    "data unavailable": "数据不可用",
    "insufficient history": "历史数据不足",
    "Confirm pair + domestic calendar": "确认配对及境内估值日历",
    "Drawdown Add Plan creation cancelled.": "已取消创建回撤加仓计划。",
    "Saved Drawdown Add Plan": "已保存回撤加仓计划",
    "No order has been placed.": "未执行任何交易。",
    "No order has been placed": "未执行任何交易",
    "No trade has been placed.": "未执行任何交易。",
    "This is a reminder only.": "这只是一条提醒。",
    "This is a reminder only": "这只是一条提醒",
    "Reminder only.": "仅作提醒。",
    "Drawdown reminder": "回撤提醒",
    "Symbol:": "代码：",
    "Name:": "名称：",
    "Asset type:": "资产类型：",
    "Triggered threshold:": "触发阈值：",
    "Recent peak:": "近期高点：",
    "Peak date:": "高点日期：",
    "Buy-plan pre-alert": "加仓计划预警",
    "Buy-plan reminder": "加仓计划提醒",
    "Buy-plan pre-alert —": "加仓计划预警 —",
    "Buy-plan reminder —": "加仓计划提醒 —",
    "Realtime estimate before close": "收盘前实时估算",
    "Market date:": "市场日期：",
    "Realtime drawdown:": "实时回撤：",
    "Recent confirmed peak:": "近期确认高点：",
    "Realtime price:": "实时价格：",
    "Quote source:": "报价来源：",
    "Quote time:": "报价时间：",
    "Tiers currently reached:": "当前到达档位：",
    "Configured additional amount:": "配置加仓金额：",
    "Long-term trend from confirmed closes": "确认收盘价的长期趋势",
    "Long-term trend": "长期趋势",
    "Final confirmation will use closing data.": "最终确认将使用收盘数据。",
    "Only after you actually subscribe, record it with:": (
        "只有实际提交申购后，才使用以下命令记录："
    ),
    "If you add today, remember to sync the fund position after units settle.": "如果今天加仓，请在份额确认后同步基金持仓。",
    "Current drawdown:": "当前回撤：",
    "Source:": "来源：",
    "Newly triggered tier": "新触发档位",
    "Newly triggered tiers": "新触发档位",
    "Total additional amount now due:": "本次应加仓总额：",
    "Regular DCA continues separately.": "常规定投继续独立执行。",
    "Price-Gain reminder": "涨幅提醒",
    "Fund:": "基金：",
    "NAV source:": "净值来源：",
    "Unit NAV:": "单位净值：",
    "Average unit cost": "平均单位成本",
    "Gain since configured position cost:": "相对配置持仓成本涨幅：",
    "Newly reached thresholds:": "新达到阈值：",
    "This is a price-gain reminder only, not an instruction to sell.": "这只是涨幅提醒，不是卖出指令。",
    "This is a price-gain reminder only.": "这只是涨幅提醒。",
    "If you redeem, remember to run /sync_position with platform values.": "如有赎回，请使用平台实际数据运行 /sync_position。",
    "Profit rate:": "涨幅：",
    "Cost:": "配置成本：",
    "Fixed DCA reminder": "固定定投提醒",
    "Fixed DCA reminders": "固定定投提醒",
    "DCA reminders": "定投提醒",
    "DCA reminder": "定投提醒",
    "Scheduled date:": "计划日期：",
    "Gross amount:": "计划总额：",
    "Total planned amount:": "本次计划合计：",
    "Estimated subscription NAV date:": "预计申购净值日期：",
    "Waiting for the next confirmed open day before estimating units.": "等待下一个确认开市日后再估算份额。",
    "Holiday policy skipped this occurrence; no position estimate will apply.": "节假日策略已跳过本期，不会应用持仓估算。",
    "The bot assumes the configured deduction executes; it does not verify it.": "Bot 假设配置扣款会执行，但无法核实。",
    "If deduction failed, use": "如果扣款失败，请使用",
    "No action is required for this configured holiday skip.": (
        "本期按节假日策略跳过，无需操作。"
    ),
    "Remember to run /sync_position after any visible platform mismatch.": "发现平台数据不一致时，请记得运行 /sync_position。",
    "Deduction failed / not executed": "扣款失败或未执行",
    "Skipped fixed DCA occurrence": "已跳过固定定投期次",
    "The occurrence is still pending; try again.": "该期次仍在处理中，请稍后重试。",
    "This estimate was already applied. Use /sync_position to correct the platform position; no units were subtracted.": "该估算已经应用。请使用 /sync_position 按平台数据修正持仓；Bot 没有扣减份额。",
    "This estimate was already applied.": "该估算已经应用。",
    "This occurrence was already reconciled by Position Sync.": "该期次已经通过持仓同步完成对账。",
    "Fixed DCA occurrence not found.": "未找到固定定投期次。",
    "Confirm zero position": "确认持仓为零",
    "Position close cancelled. Nothing changed.": "已取消清仓确认，未作任何修改。",
    "Tracked position set to zero; position cycle closed.": (
        "已将跟踪持仓设为零，并结束本持仓周期。"
    ),
    "Position was already zero; nothing changed.": "持仓已经为零，未作任何修改。",
    "Partially redeemed": "部分赎回",
    "Fully closed": "全部清仓",
    "No action": "不操作",
    "fund-alert-bot test": "fund-alert-bot 测试",
    "Test notification": "测试通知",
    "Source: fund-alert-bot": "来源：fund-alert-bot",
    "Purpose: channel connectivity check": "用途：通知渠道连通性检查",
    "Sent test notification to": "测试通知已发送至",
    "channel(s).": "个渠道。",
}

_EN_TO_ZH.update(
    {
        "Invalid asset_type:": "无效的 asset_type：",
        ". Valid values:": "。有效值：",
        "symbol must not be empty": "代码不能为空",
        "name must not be empty": "名称不能为空",
        "lookback_days must be a positive integer": "lookback_days 必须是正整数",
        "auto cost is only valid for cn_open_fund": "auto 成本仅适用于 cn_open_fund",
        "auto thresholds must be unique and strictly ascending": "auto 阈值必须唯一并严格递增",
        "thresholds must be comma-separated percentages": "阈值必须是逗号分隔的百分数",
        "thresholds must be greater than 0 and less than 100": "阈值必须大于 0 且小于 100",
        "cost must be a positive number": "成本必须是正数",
        "fee must use rate:<percent>% or fixed:<RMB>": "手续费必须使用 rate:<percent>% 或 fixed:<RMB>",
        "fee must be a finite non-negative number": "手续费必须是有限的非负数",
        "cutoff must use 24-hour HH:MM format": "截止时间必须使用 24 小时 HH:MM 格式",
        "units and average_unit_cost must be finite non-negative numbers": "份额和 average_unit_cost 必须是有限的非负数",
        "use positive units with positive cost, or exact 0 0 for a closed position": "正持仓必须使用正份额和正成本；完全清仓必须准确输入 0 0",
        "plan_id must be a positive integer": "plan_id 必须是正整数",
        "rule_id must be a positive integer": "rule_id 必须是正整数",
        "Rule id must be an integer": "规则 ID 必须是整数",
        "rule_id must be an integer and date YYYY-MM-DD": "rule_id 必须是整数，日期必须使用 YYYY-MM-DD",
        "tier percentages must be comma-separated numbers": (
            "档位百分比必须是逗号分隔的数字"
        ),
        "tier percentages must be unique numbers between 0 and 100": "档位百分比必须是 0 到 100 之间且不重复的数字",
        "Selected tiers must be unique.": "所选档位不能重复。",
        "One or more selected tiers are not configured.": "一个或多个所选档位未配置。",
        "Selected tiers are not all present in a same-day reminder.": "所选档位并非全部来自同一天的提醒。",
        "Selected tiers are not all present in that dated reminder.": "所选档位并非全部来自该日期的提醒。",
        "No eligible tiers were selected.": "没有选择可记录的档位。",
        "One or more selected tiers were already recorded.": "一个或多个所选档位已经记录。",
        "No eligible same-day plan reminder was found.": (
            "未找到当天可操作的计划提醒。"
        ),
        "For a past addition, use /mark_added <plan_id> <tier_percentages> <YYYY-MM-DD>.": "如果是补记历史加仓，请使用 /mark_added <plan_id> <tier_percentages> <YYYY-MM-DD>。",
        "No eligible plan reminder containing these tiers was found on ": "未在以下日期找到包含这些档位的有效计划提醒：",
        "This reminder expired or its peak cycle changed.": (
            "该提醒已过期或高点周期已改变。"
        ),
        "Selected tiers:": "所选档位：",
        "Configured gross total:": "配置总金额：",
        "Position estimate readiness:": "持仓估算准备状态：",
        "Confirm a completed manual addition": "确认已完成的手动加仓",
        "Continue only if you already submitted the fund subscription.": "仅在已经提交基金申购后继续。",
        "The bot records your statement; it does not place or verify an order.": "Bot 只记录你的确认，不会下单或核实订单。",
        "only trailing lookback:<calendar_days> is allowed": "仅允许在末尾使用 lookback:<calendar_days>",
        "lookback must be a positive integer": "lookback 必须是正整数",
        "tiers must use percent:amount separated by commas": "档位必须使用逗号分隔的 percent:amount 格式",
        "tier percent and amount must be numbers": "档位百分比和金额必须是数字",
        "tier percent must be greater than 0 and less than 100": "档位百分比必须大于 0 且小于 100",
        "tier amount must be a positive finite number": "档位金额必须是有限正数",
        "fund_symbol must be exactly 6 digits": "fund_symbol 必须正好是 6 位数字",
        "holiday policy must be holiday:next or holiday:skip": "节假日策略必须是 holiday:next 或 holiday:skip",
        "fixed fee must be lower than the DCA amount": "固定手续费必须低于定投金额",
        "amount must be a positive number": "金额必须是正数",
        "Subscription cutoff:": "申购截止时间：",
        "auto position cost": "自动持仓成本",
        "average cost": "平均成本",
        "reached": "已达到",
        "Latest occurrence:": "最近期次：",
        "NAV date": "净值日期",
        "last sync": "最近同步",
        "later estimates": "后续估算",
        "using NAV": "使用净值",
        "Price-Gain setup available for": "可为以下基金配置涨幅提醒：",
        "Template:": "命令模板：",
        "add recorded (user-confirmed)": "已记录加仓（用户确认）",
        "reminded; no add recorded": "已提醒；未记录加仓",
        "Position sync required since": "需要同步持仓，起始日期",
        "No enabled drawdown_from_high rules to check": "没有已启用的普通回撤规则可检查",
        (
            "No enabled drawdown_from_high, profit_reminder, or dca_reminder "
            "rules to check"
        ): "没有已启用的普通回撤、涨幅或定投规则可检查",
        "No market data available for": "没有可用行情数据：",
        "Market data is empty.": "行情数据为空。",
        "Market data is missing price field:": "行情数据缺少价格字段：",
        "Market data is missing date field.": "行情数据缺少日期字段。",
        "Market data contains invalid dates.": "行情数据包含无效日期。",
        "Market data has no prices in the lookback window.": "回看窗口内没有价格数据。",
        "Reminder: this is not automatic trading and no orders will be placed.": "提醒：这不是自动交易，不会执行任何订单。",
        "Latest:": "最新价：",
        "Latest price:": "最新价：",
        "Latest NAV:": "最新净值：",
        "Before-close estimate": "收盘前估算",
        "After-close confirmation": "收盘后确认",
        "Feeder-fund NAV settlement": "联接基金净值结算",
        "Feeder-fund data unavailable": "联接基金数据不可用",
        "Drawdown plan data unavailable": "回撤加仓计划数据不可用",
        "could not evaluate:": "无法评估：",
        "Pending position work was not applied and no Price-Gain decision was made.": "待处理持仓变动未应用，也未作出涨幅提醒判断。",
        "No tier decision was made for these plans.": "这些计划未作出档位判断。",
        "Please check your own platform.": "请自行检查销售平台。",
        "Only after you actually subscribe, record it with:": "只有实际申购后，才使用以下命令记录：",
        (
            "Delayed confirmation for an earlier trading day; action buttons "
            "are unavailable."
        ): "这是较早交易日的延迟确认，操作按钮不可用。",
        "If you bought, wait for the fund platform to settle, then run /sync_position.": "如已购买，请等待平台结算后运行 /sync_position。",
        "Effective date": "生效日期",
        "is not a confirmed open day.": "不是已确认开市日。",
        "Confirmed CN market calendar is required.": "需要已确认的中国市场交易日历。",
        "Exact Eastmoney fund NAV for": "东方财富准确基金净值不可用：",
        "The market-data provider cannot verify the fund's domestic calendar.": "行情提供方无法验证基金的境内估值日历。",
        "Unable to verify the fund's domestic calendar from metadata:": "无法通过元数据验证基金的境内估值日历：",
        "No rule was created; try again later.": "未创建规则，请稍后重试。",
        "Fund type": "基金类型",
        "does not use the domestic CN valuation calendar": "不使用境内中国估值日历",
        "Read-only preview unavailable:": "只读预览不可用：",
        "Read-only preview: no configured threshold is reached.": "只读预览：尚未达到任何配置阈值。",
        "Currently reached:": "当前已达到：",
        "Preview only; no threshold was consumed and no trade occurred.": "仅作预览；未消耗阈值，也未执行交易。",
        "Verified fund type:": "已验证基金类型：",
        "Weekly due day:": "每周到期日：",
        "Shared subscription fee:": "共享申购手续费：",
        "Position readiness:": "持仓准备状态：",
        "Remember: run /sync_position before automatic estimates can apply.": "请记得：应用自动估算前先运行 /sync_position。",
        "Run /sync_position before automatic estimates can apply.": "应用自动估算前，请先运行 /sync_position。",
        "future amount to": "未来金额更新为",
        "Existing occurrences are unchanged.": "已有期次保持不变。",
        "Unable to scope the confirmation; rerun command.": (
            "无法确定确认范围，请重新运行命令。"
        ),
        "Plan conflict: enabled plan id=": "计划冲突：已启用计划 id=",
        "already uses this Reference ETF or Investment Feeder Fund.": "已经使用该参考 ETF 或实际联接基金。",
        "This plan confirmation expired or belongs to another chat.": "该计划确认已过期或属于其他聊天。",
        "Plan was not saved:": "计划未保存：",
        "The first scheduled confirmed-close evaluation will initialize its cycle.": "首次定时收盘确认将初始化其周期。",
        "Unable to scope this action.": "无法确定本次操作范围。",
        "No addition recorded. The tier reminder state was not changed.": "未记录加仓，档位提醒状态未改变。",
        "Already deferred for today. These tiers remain pending for the next market date. No order has been placed.": "今天已经顺延提醒。这些档位仍待处理，下一市场日会继续提醒。未执行任何交易。",
        "Deferred for today. These tiers remain pending and will be reminded again on the next market date if still reached. No order has been placed.": "今天已顺延提醒。这些档位仍待处理，如果下一市场日仍达到条件，Bot 会再次提醒。未执行任何交易。",
        "These tiers are already skipped for this cycle. No reminder state was changed.": "这些档位在本周期已经跳过，提醒状态未改变。",
        "Skip cancelled. Nothing was changed.": "已取消跳过，未作任何修改。",
        "Already skipped for this drawdown cycle. Nothing was changed.": "这些档位在本回撤周期已经跳过，未作任何修改。",
        "Skipped for this drawdown cycle. These tiers will not be reminded again until a new cycle. No order has been placed.": "已跳过本回撤周期。这些档位在新周期前不会再次提醒。未执行任何交易。",
        "Invalid skip action.": "无效的跳过操作。",
        "Invalid defer action.": "无效的顺延提醒操作。",
        "Confirm skipping all currently actionable tiers? They will not be reminded again in this drawdown cycle.": "确认跳过当前全部可执行档位？本回撤周期内不会再次提醒。",
        "Choose which actionable tiers to skip for this drawdown cycle.": "请选择本回撤周期要跳过的可执行档位。",
        "Confirm skipping ": "确认跳过 ",
        "This tier will not be reminded again in the current drawdown cycle.": "本回撤周期内不会再次提醒该档位。",
        "This reminder expired after its market date.": "该提醒已超过市场日期而失效。",
        "The action date is not a confirmed CN open day.": (
            "操作日期不是已确认的中国开市日。"
        ),
        "Addition was not recorded:": "未记录加仓：",
        "These tiers were already recorded; no duplicate was created.": "这些档位已经记录，没有创建重复记录。",
        "Recorded tiers": "已记录档位",
        "Position sync is required.": "需要同步持仓。",
        "waiting for exact dated NAV on": "等待以下日期的准确净值：",
        "The bot did not place or verify an order.": "Bot 未下单，也未核实订单。",
        "Invalid DCA skip action.": "无效的定投跳过操作。",
        "Confirm failed DCA deduction": "确认定投扣款失败",
        "Confirm failed deduction": "确认扣款失败",
        "Confirm only if this deduction failed or was not executed.": "仅在本期扣款失败或未执行时确认。",
        "This occurrence will not update the estimated position.": "本期不会更新估算持仓。",
        "DCA skip cancelled. Nothing was changed.": "已取消跳过本期定投，未作任何修改。",
        "Confirm rule removal": "确认移除规则",
        "Confirm removal": "确认移除",
        "Rule removal cancelled. Nothing changed.": "已取消移除规则，未作任何修改。",
        "Invalid rule removal action.": "无效的规则移除操作。",
        "No reminder state will change until you confirm.": "确认前不会修改任何提醒状态。",
        "Historical correction: no NAV, units, or cost estimate will be created.": "历史补记不会创建净值、份额或成本估算。",
        "Choose whether the current exact position already includes these subscriptions.": "请选择当前精确持仓是否已包含这些申购。",
        "After the platform settles, run /sync_position with its exact values.": "平台结算后，请使用准确数据运行 /sync_position。",
        "Historical additions cannot create position estimates.": "历史补记不能创建持仓估算。",
        "Actual subscription date:": "实际申购日期：",
        "action_date must use YYYY-MM-DD": "action_date 必须使用 YYYY-MM-DD",
        "action_date must not be in the future": "action_date 不能是未来日期",
        "Partial redemption — sync position": "部分赎回——同步持仓",
        "Confirm fully closed": "确认已全部清仓",
        "No position action": "不调整持仓",
        "No position action was recorded.": "未记录任何持仓操作。",
        "The reached threshold was already recorded when this reminder was created and will not repeat in the current position cycle.": "该阈值在提醒创建时已经记录，本持仓周期内不会重复提醒。",
        "If you actually subscribed, record it using the actual date:": "如果已经实际申购，请使用真实申购日期记录：",
        "If the deduction failed, use:": "如果扣款失败，请使用：",
        "If executed, wait for a confirmed subscription NAV date.": "如果已经扣款，请等待确认申购净值日期。",
        "Next action: none; use /sync_position only if the platform differs.": "下一步：无需操作；仅在平台数据不一致时使用 /sync_position。",
        "Configuration invalid; inspect logs or recreate this rule.": "配置无效；请检查日志或重新创建该规则。",
        "This addition confirmation expired or belongs to another chat.": "该加仓确认已过期或属于其他聊天。",
        "Manual addition recording cancelled.": "已取消记录手动加仓。",
        "The confirmation is at or after the configured cutoff.": "当前确认时间已达到或超过配置的截止时间。",
        "When did you actually submit the fund subscription?": (
            "你实际何时提交基金申购？"
        ),
        "This cutoff confirmation expired.": "截止时间确认已过期。",
        "The change applies only to future manual confirmations.": "本次修改只影响未来的手动确认。",
        "Unable to scope the position confirmation.": "无法确定持仓确认范围。",
        "Pending additions exist. Classify this platform snapshot:": "存在待处理加仓，请说明平台快照包含情况：",
        "Review all pending additions listed above before choosing.": "选择前请核对上面列出的所有待处理加仓。",
        "This position confirmation expired.": "持仓确认已过期。",
        "Position sync cancelled.": "已取消持仓同步。",
        "Nothing was changed.": "未作任何修改。",
        "Pending estimates remain eligible for dated-NAV processing.": "待处理估算仍会等待准确日期净值。",
        "Pending fixed DCA estimates remain eligible for NAV processing.": "待处理固定定投估算仍会继续等待净值。",
        "After the platform confirms the redemption, run:": "平台确认赎回后，请运行：",
        "Nothing was changed by this button.": "此按钮未修改任何数据。",
        "Recorded: no position update.": "已记录：不更新持仓。",
        "Confirm only after the platform shows zero units.": "仅在平台显示份额为零后确认。",
        "Price-Gain setup is no longer available.": "涨幅提醒设置已不可用。",
        "Edit the threshold placeholder, then send this separate command:": "请修改阈值占位符，然后单独发送此命令：",
        "No rule was created by this button.": "此按钮未创建规则。",
        "Notification delivery failures:": "通知投递失败：",
        "Set gain thresholds —": "设置涨幅阈值 —",
        "Deduction failed —": "扣款失败 —",
        "Reference ETF data: unavailable": "参考 ETF 数据：不可用",
        "Feeder-fund NAV: unavailable": "联接基金净值：不可用",
        "Position value: unavailable": "持仓市值：不可用",
        "unavailable (insufficient history)": "不可用（历史数据不足）",
        "trend: falling": "趋势：下降",
        "trend: rising": "趋势：上升",
        "Updated fund": "已更新基金",
        "Updated DCA rule id=": "已更新定投规则 id=",
        "Added drawdown rule id=": "已添加回撤规则 id=",
        "Added profit rule id=": "已添加涨幅规则 id=",
        "Added auto-cost Price-Gain rule id=": "已添加自动成本涨幅规则 id=",
        "Added DCA rule id=": "已添加定投规则 id=",
        "Added fixed DCA rule id=": "已添加固定定投规则 id=",
        "Saved Drawdown Add Plan id=": "已保存回撤加仓计划 id=",
        "Disabled drawdown plan id=": "已停用回撤加仓计划 id=",
        "Disabled fixed DCA rule id=": "已停用固定定投规则 id=",
        "Disabled auto-cost Price-Gain rule id=": "已停用自动成本涨幅规则 id=",
        "Deleted rule id=": "已删除规则 id=",
        "Disabled rule id=": "已停用规则 id=",
        "Rule id=": "规则 id=",
        " is already disabled.": " 已经停用。",
        " was not found": " 未找到",
    }
)

_EXACT_EN_TO_ZH = {"Cancel": "取消"}

_ZH_TO_EN = {
    "实际金额与配置总额完全一致": "Actual amount exactly matches the configured total",
    "金额不同，记录档位后同步持仓": "Different amount; record tiers then sync position",
    "记录档位，稍后同步持仓": "Record tiers and sync position later",
    "记录历史档位，稍后同步持仓": "Record historical tiers and sync position later",
    "当前精确持仓已包含，仅补记档位": "Current exact position already includes it; record tiers only",
    "前已提交 — 当日净值": " submitted before cutoff — same-day NAV",
    "后才提交 — 下一开放日": " submitted after cutoff — next open-day NAV",
    "已全部包含": "All included",
    "均未包含": "None included",
    "只包含部分（取消）": "Partially included (cancel)",
    "✅ 已按全部档位加仓": "✅ Added all tiers",
    "✅ 已加仓": "✅ Added",
    "✅ 全部已加仓": "✅ Record all tiers as added",
    "仅记录 ": "Record only ",
    "⏭ 暂未加仓": "⏭ No addition yet",
    "⏰ 今天不投，之后提醒": "⏰ Not today, remind me again",
    "⏭ 本周期跳过": "⏭ Skip for this cycle",
    "全部跳过": "Skip all",
    "确认跳过": "Confirm skip",
    "确认全部跳过": "Confirm skip all",
    "跳过 -": "Skip -",
    "↩️ 返回选择": "↩️ Back to choices",
    "❌ 取消": "❌ Cancel",
    "• 标的：": "• Asset: ",
    "• 日期：": "• Date: ",
    "• 计划金额：": "• Planned amount: ",
    " 元": " RMB",
    "提醒：这是纪律提醒，不会自动交易。": "Reminder: this is a discipline reminder; no automatic trade occurs.",
}

_EXACT_ZH_TO_EN = {"取消": "Cancel"}

_DECORATIONS = (
    "📉 ",
    "💰 ",
    "⚠️ ",
    "✅ ",
    "⏭ ",
    "♻️ ",
    "❌ ",
    "👌 ",
    "📋 ",
    "📈 ",
    "🎯 ",
    "⏳ ",
    "🆕 ",
    "⏰ ",
    "📊 ",
    "🔔 ",
    "🧪 ",
    "• ",
)
_DYNAMIC_PREFIXES = (
    "Added drawdown rule id=",
    "Added profit rule id=",
    "Added auto-cost Price-Gain rule id=",
    "Added DCA rule id=",
    "Added fixed DCA rule id=",
    "Saved Drawdown Add Plan id=",
    "Updated DCA rule id=",
    "Updated fund",
    "Disabled drawdown plan id=",
    "Disabled fixed DCA rule id=",
    "Disabled auto-cost Price-Gain rule id=",
    "Deleted rule id=",
    "Disabled rule id=",
    "Rule id=",
    "Skipped fixed DCA occurrence",
    "Set gain thresholds —",
    "Deduction failed —",
    "Buy-plan pre-alert —",
    "Buy-plan reminder —",
    "Checked ",
    "已检查 ",
    "Read-only Drawdown Add Plans checked: ",
    "Record only ",
    "仅记录 ",
    "跳过 -",
    "Confirm skipping ",
    "No eligible plan reminder containing these tiers was found on ",
)
_LABEL_VALUE_SUFFIXES = {
    "Gross amount:": ("RMB",),
    "Lookback:": ("calendar days", "days"),
    "Position value:": ("unavailable (unit NAV could not be fetched)",),
    "Total planned amount:": ("RMB",),
}
_LABEL_VALUE_TRANSLATIONS = {
    "Holiday policy:": {
        "next": "顺延至下一开放日",
        "skip": "跳过",
    },
    "Status:": {"enabled": "已启用", "disabled": "已停用"},
    "Action:": {
        "soft-disable": "停用并保留历史",
        "permanently delete": "永久删除",
    },
}
_DATED_LABELS = {"Peak:", "Latest:"}


def set_language(language: str) -> None:
    """Set the process-wide output language after validated startup config."""
    global _language
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("BOT_LANGUAGE must be one of: zh-CN, en")
    _language = language


def get_language() -> str:
    """Return the active output language."""
    return _language


def _localize_label_value(label: str, value: str, replacements: dict[str, str]) -> str:
    if _language == "en" and label == "• 计划金额：":
        value = value.removesuffix(" 元") + " RMB"
    elif _language == "zh-CN":
        value = _LABEL_VALUE_TRANSLATIONS.get(label, {}).get(value.strip(), value)
        dated_value, separator, dated_on = value.rpartition(" on ")
        if (
            label in _DATED_LABELS
            and separator
            and len(dated_on) == 10
            and dated_on[4:5] == dated_on[7:8] == "-"
            and dated_on.replace("-", "").isdigit()
        ):
            value = f"{dated_value}，日期：{dated_on}"
        suffix = next(
            (
                item
                for item in _LABEL_VALUE_SUFFIXES.get(label, ())
                if value.endswith(item)
            ),
            None,
        )
        if suffix is not None:
            value = value.removesuffix(suffix) + replacements[suffix]
    return replacements[label] + value


def localize_text(text: str) -> str:
    """Translate UI structure without rewriting interpolated user values."""
    return "\n".join(_localize_line(line) for line in text.split("\n"))


def _localize_line(line: str) -> str:
    exact = _EXACT_EN_TO_ZH if _language == "zh-CN" else _EXACT_ZH_TO_EN
    if line in exact:
        return exact[line]
    replacements = _EN_TO_ZH if _language == "zh-CN" else _ZH_TO_EN
    if line in replacements:
        return replacements[line]

    if line.startswith("/") and " - " in line:
        command, description = line.split(" - ", 1)
        return f"{command} - {replacements.get(description, description)}"

    full_line_label = next(
        (
            source
            for source in sorted(replacements, key=len, reverse=True)
            if source.endswith((":", "：")) and line.startswith(source)
        ),
        None,
    )
    if full_line_label is not None:
        value = line[len(full_line_label) :]
        return _localize_label_value(full_line_label, value, replacements)

    decoration = next((item for item in _DECORATIONS if line.startswith(item)), "")
    content = line[len(decoration) :]
    if content in replacements:
        return decoration + replacements[content]

    if _language == "zh-CN" and content.startswith("Updated DCA rule id="):
        before_amount, separator, after_amount = content.rpartition(
            " future amount to "
        )
        if separator:
            amount, suffix, _ = after_amount.partition(
                ". Existing occurrences are unchanged."
            )
            if suffix:
                return (
                    decoration
                    + before_amount.replace(
                        "Updated DCA rule id=", replacements["Updated DCA rule id="], 1
                    )
                    + " 未来金额更新为 "
                    + amount
                    + "。已有期次保持不变。"
                )

    if _language == "zh-CN" and content.startswith("Saved Drawdown Add Plan id="):
        identity, separator, after_identity = content.partition(": ETF ")
        reference, fund_separator, after_fund = after_identity.partition(" → fund ")
        fund, suffix, _ = after_fund.partition(
            ". The first scheduled confirmed-close evaluation will initialize "
            "its cycle. No order has been placed."
        )
        if separator and fund_separator and suffix:
            return (
                decoration
                + identity.replace(
                    "Saved Drawdown Add Plan id=",
                    replacements["Saved Drawdown Add Plan id="],
                    1,
                )
                + f"：ETF {reference} → 基金 {fund}。"
                + "首次定时收盘确认将初始化其周期。未执行任何交易。"
            )

    if (
        _language == "zh-CN"
        and content.startswith("Sent test notification to ")
        and content.endswith(" channel(s).")
    ):
        channels = content.removeprefix("Sent test notification to ").removesuffix(
            " channel(s)."
        )
        if channels:
            return (
                decoration
                + f"测试通知已发送至 {channels.replace(' of ', '/')} 个渠道。"
            )

    if _language == "en":
        cutoff_suffix = next(
            (
                source
                for source in ("前已提交 — 当日净值", "后才提交 — 下一开放日")
                if content.endswith(source)
                and len(content.removesuffix(source)) == 5
                and content.removesuffix(source)[2:3] == ":"
                and content.removesuffix(source).replace(":", "").isdigit()
            ),
            None,
        )
        if cutoff_suffix is not None:
            cutoff = content.removesuffix(cutoff_suffix)
            return decoration + cutoff + replacements[cutoff_suffix]

    label = next(
        (
            source
            for source in sorted(replacements, key=len, reverse=True)
            if source.endswith((":", "：")) and content.startswith(source)
        ),
        None,
    )
    if label is not None:
        return decoration + _localize_label_value(
            label, content[len(label) :], replacements
        )

    dynamic_prefix = next(
        (source for source in _DYNAMIC_PREFIXES if content.startswith(source)),
        None,
    )
    if dynamic_prefix is not None and dynamic_prefix in replacements:
        remainder = content[len(dynamic_prefix) :]
        if _language == "zh-CN" and remainder.endswith(" was not found"):
            remainder = (
                remainder.removesuffix(" was not found")
                + replacements[" was not found"]
            )
        elif _language == "zh-CN" and remainder.endswith(" is already disabled."):
            remainder = (
                remainder.removesuffix(" is already disabled.")
                + replacements[" is already disabled."]
            )
        return decoration + replacements[dynamic_prefix] + remainder

    # Prose lines contain no configured identifier field. Translate complete
    # leading sentences, then continue with the remaining prose.
    sentence = next(
        (
            source
            for source in sorted(replacements, key=len, reverse=True)
            if source.endswith(".") and content.startswith(source)
        ),
        None,
    )
    if sentence is not None:
        remainder = content[len(sentence) :]
        separator = remainder[: len(remainder) - len(remainder.lstrip())]
        return (
            decoration
            + replacements[sentence]
            + separator
            + _localize_line(remainder.lstrip())
        )
    return line


def localize_actions(
    actions: tuple[tuple[tuple[str, str], ...], ...],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Translate Telegram button labels without changing callback identities."""
    return tuple(
        tuple((localize_text(label), callback_data) for label, callback_data in row)
        for row in actions
    )
