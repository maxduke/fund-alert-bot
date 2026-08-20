# fund-alert-bot

[English](README.md) | [简体中文](README.zh-CN.md)

一个轻量、个人使用的投资提醒 Bot。

本项目与 `maxduke/rsi6_monitor_bot` 分开运行：RSI6 提醒仍由原项目负责；
本 Bot 只负责：

- 相对近期高点的回撤提醒
- 固定定投提醒与联接基金持仓估算
- 基于配置成本的涨幅提醒
- Telegram、Bark、ntfy、Webhook 通知

它不是交易系统，不连接券商，不会自动买入、卖出或调仓，也不提供投资建议。

## 当前状态

项目已经实现 Python 3.12 单进程服务、SQLite 持久化、APScheduler 调度、
AKShare 行情归一化、Telegram 命令、通知投递状态与失败恢复、Docker 镜像、
Ruff 和 pytest 测试。

已实现的 Telegram 命令：

- `/start`
- `/help`
- `/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>`
- `/add_profit <asset_type> <symbol> <name> <cost|auto> <thresholds>`
- `/add_dca <name> <weekday> <amount>`
- `/add_dca <fund_symbol> <name> <weekday> <gross_amount> <fee> [holiday:next|holiday:skip]`
- `/set_dca_amount <rule_id> <new_amount>`
- `/dca_skip <rule_id> <due_date>`
- `/set_fund_fee <fund_symbol> <rate:<percent>%|fixed:<RMB>>`
- `/set_fund_cutoff <fund_symbol> <HH:MM>`
- `/sync_position <fund_symbol> <units> <average_unit_cost>`
- `/add_drawdown_plan <reference_etf> <feeder_fund> <name> <tiers> [lookback:<days>]`
- `/mark_added <plan_id> <tier_percentages>`
- `/plans [refresh]`
- `/list`
- `/del <id>`
- `/check`
- `/test_notify`

## 回撤提醒

普通回撤规则支持 `cn_index`、`cn_etf`、`cn_stock` 和 `cn_open_fund`。
阈值按百分数输入，例如 `10,15,20`。`/check` 会立即检查已启用的规则，
并显示当前相对近期高点的回撤。

调度器在交易日收盘前运行实时估算，在收盘后使用确认数据检查。若能从
AKShare 的新浪交易日历取得官方开市状态，节假日会跳过；日历不可用时，
普通定时检查退回到工作日判断，并在日志中明确记录。

## Price-Gain 涨幅提醒

固定成本示例：

```text
/add_profit cn_etf 159915 ChiNext-ETF 1.85 25,40
```

联接基金当前持仓成本示例：

```text
/add_profit cn_open_fund 110026 "A500 feeder" auto 20,30
```

数字成本是用户配置的固定成本；`auto` 使用 `/sync_position` 保存的平均单位
成本。持仓型提醒只使用联接基金准确日期的单位净值，不使用参考 ETF 价格。
阈值在一个连续的正持仓周期内只提醒一次；`/sync_position <symbol> 0 0`
会关闭该持仓周期，之后重新持有才开始新周期。

提醒按钮只能帮助记录部分同步、二次确认清仓或不操作，不会赎回基金。

## 固定定投

只提醒、不估算持仓：

```text
/add_dca 创业板 周四 1000
```

支持周一至周日以及 Monday 至 Sunday。每天的调度检查只会为同一规则、
同一日期发送一次提醒。

需要按准确净值估算联接基金份额时，使用增强形式：

```text
/add_dca 110026 "A500 feeder" 周四 2000 rate:0.12% holiday:next
```

- `holiday:next`：节假日顺延到下一个确认开市日，保留原到期日作为记录身份。
- `holiday:skip`：节假日直接记录为跳过，不增加持仓估算。
- 平台未执行扣款时，点击 Telegram 失败按钮，或在净值处理前运行：

```text
/dca_skip <rule_id> <YYYY-MM-DD>
```

Bot 假设配置的固定扣款会执行，但无法向销售平台核实。发现差异时必须重新
运行 `/sync_position`。创建增强规则时会进行一次单基金类型检查；QDII、
海外基金或无法确认国内估值日历的基金不会创建规则。该检查不会使用东方财富
全量基金列表。

同一天到期的多条增强固定定投会合并为一条通知。每只基金的 occurrence、
份额估算和“扣款失败”按钮仍然独立；`holiday:skip` 项会显示，但不计入通知中的
合计金额。投递失败或进程重启后的重试也使用相同的合并规则。

### 修改未来定投金额

```text
/set_dca_amount 12 500
```

这会保留规则 12 的 ID，只修改以后新建的 occurrence。已经生成、等待净值、
已经应用或属于历史的 occurrence 均保留原金额、手续费快照、提醒内容和持仓
估算。若配置的是固定手续费，手续费必须低于新金额。

## 联接基金手续费与持仓同步

手续费按实际持有的联接基金共享，而不是按单个 DCA 规则保存：

```text
/set_fund_fee 110026 rate:0.15%
/set_fund_fee 110026 fixed:1.50
```

`rate:0%` 可用于没有前端申购费的份额类别。手续费变更只影响之后新建的定投
occurrence 和手动加仓估算；历史快照不会重算。因为它是基金级设置，同一基金
未来的固定定投与手动回撤加仓都会使用新手续费。

默认申购截止时间是 `15:00`，平台不同时可修改：

```text
/set_fund_cutoff 110026 15:00
```

首次使用持仓估算前，从销售平台复制准确份额和平均单位成本：

```text
/sync_position 110026 12345.67 1.2345
```

两者必须同时为正。只有完全清仓时才使用：

```text
/sync_position 110026 0 0
```

同步会替换 Bot 当前快照并标记为准确数据；它不会导入交易记录，也不会联系
券商。正持仓响应会读取联接基金最新已发布净值，并显示净值日期和估算市值。

以下情况必须再次同步：赎回、分红或再投资、未被 Bot 记录的购买、手续费
不符，或平台显示与 Bot 不一致。Bot 没有券商连接，无法自动发现这些变化。

## 回撤加仓计划

回撤加仓计划用上市 ETF 作为市场参考，用你实际持有的 ETF 联接基金作为持仓
身份：

```text
/add_drawdown_plan 510300 000001 "Core index" 15:5000,20:10000,25:15000
```

可选的 `lookback:<days>` 只能放在最后，默认 365 个日历日。MA250 和 20 个
交易日的 MA250 斜率只提供趋势背景，绝不会独立触发、取消或修改加仓金额。

每档金额是增量。例如上面的最大单周期额外加仓总额为 ¥30,000；若价格直接
跨过 15%、20%、25% 三档，会发送一条合并提醒，总额仍为 ¥30,000。

命令先显示只读预览，不立即保存。确认两个六位代码确实是参考 ETF 与联接基金，
且联接基金遵循国内 A 股估值日历后，才点击确认按钮。确认在 10 分钟后过期，
并绑定当前 Telegram 用户和聊天。

收盘确认只使用参考 ETF 的前复权（`qfq`）日线；联接基金净值不会代替 ETF
回撤，ETF 实时价格也不会被当作准确持仓价值。每档在同一个高点周期内只记录
一次。提醒不等于已经购买。

- `/plans`：简洁查看所有计划，默认读取 SQLite 中已保存的数据。
- `/plans refresh`：明确要求刷新 ETF 收盘历史和联接基金净值，可能产生行情接口请求。
- `/check`：查看详细回撤、MA250、档位和持仓状态。

如果本地确认收盘数据已经达到尚未记录的档位，`/plans` 会明确显示“已达到，等待收盘确认”。
收盘前实时估算不会消耗正式档位，必须由收盘后的 ETF 收盘价确认。

对于回撤加仓计划，这两条命令不会消耗正式档位或创建计划提醒。但 `/check` 也会
评估普通回撤、固定成本涨幅和当天到期的 DCA 规则，满足条件时仍可能发送提醒。

默认 `14:50` 使用 ETF 实时价格发送临时预警，不消耗正式档位。如果你确实
提交了联接基金申购，可点击 Telegram 按钮或使用：

```text
/mark_added 1 15,20
```

只确认你实际提交的档位和配置金额。截止时间前提交时，Bot 等待该市场日期的
准确基金净值；截止时间后会询问实际申购发生在截止前还是截止后。配置完整且
金额相符时，准确日期净值发布后会更新“估算”持仓；金额不同或配置不完整时，
只记录档位，之后必须 `/sync_position`。

每天 `08:30` 的净值任务在自然日运行，因为交易日净值可能延迟发布。待处理估算
会请求准确日期净值；存在正持仓的已启用 `auto` 涨幅规则也会使用最近完成交易日
的准确净值评估。缺失的结算数据保持 pending，不会使用过期净值猜测。

更完整的周期、实时预警、数据源与异常行为见
[中文投资计划操作指南](docs/investment-plan-guide.zh-CN.md)。

## 数据源与请求效率

- ETF 收盘确认：AKShare 东方财富前复权历史数据；失败时关闭本次确认，不猜测。
- ETF 收盘前估算：每个参考 ETF 一次有界东方财富请求；失败后进入短暂全局
  冷却，其余计划不重复打同一来源；随后使用新浪单标的备用。新浪失败冷却按
  ETF 代码隔离，一个标的的临时失败不会压掉其他计划；两级来源都失败时，通知会
  同时列出东方财富和新浪的原因。
- 两个实时来源都必须返回自身报价时间，过期数据不能当作当前行情。
- 联接基金准确净值没有可等价替代的新浪接口；失败时保持 pending。
- Bot 不轮询实时接口，只执行一次配置的收盘前任务，并复用同一轮缓存。

确认后的标准化 ETF 日线和准确的联接基金净值会额外保存到 SQLite。首次补齐历史后，
收盘任务会刷新完整的 QFQ 所需历史窗口（未复权历史使用少量重叠，并带少量日历缓冲），普通 `/plans` 直接读取本地数据；收盘前会先确认历史覆盖到最近一个已确认交易日；只有显式使用
`/plans refresh` 才要求刷新行情，避免重复消耗东方财富代理积分。

## 通知与持久化

Telegram 是命令通道和默认通知通道。Bark、ntfy、Webhook 可通过环境变量
启用。提醒先在 SQLite 中保留，再投递；失败或进程中断后的未投递提醒会在下次
启动或晨间任务恢复。投递失败不会重复创建定投 occurrence 或回撤档位记录。

Telegram 回复、按钮以及所有通知渠道共用一个全局语言。默认
`BOT_LANGUAGE=zh-CN`；如需英文，改为 `BOT_LANGUAGE=en` 并重启服务。
`/check` 等 Telegram 命令名称始终保持英文。
Bot 启动时会注册命令菜单；在 Telegram 输入 `/` 即可看到可用命令及本地化说明。

默认调度配置：

- `TZ=Asia/Shanghai`
- `BOT_LANGUAGE=zh-CN`
- `AFTER_CLOSE_CHECK_TIME=17:10`
- `BEFORE_CLOSE_CHECK_TIME=14:50`
- `DCA_REMINDER_TIME=09:30`
- `FUND_NAV_PROCESS_TIME=08:30`
- `AKSHARE_RETRIES=3`
- `AKSHARE_RETRY_DELAY_SECONDS=0.5`
- `AKSHARE_LATEST_LOOKBACK_DAYS=45`

可选通知配置：

- `BARK_ENABLED`、`BARK_SERVER_URL`、`BARK_DEVICE_KEY`
- `NTFY_ENABLED`、`NTFY_SERVER_URL`、`NTFY_TOPIC`
- `WEBHOOK_ENABLED`、`WEBHOOK_URL`

### 可选的付费东方财富代理

东方财富接口可能严格限流。镜像已固定包含
[`akshare-proxy-patch==0.5.0`](https://github.com/HelloYie/akshare-proxy-patch)，
但默认关闭。只有购买代理服务并拿到 Token
后才启用：

```dotenv
AKSHARE_PROXY_ENABLED=true
AKSHARE_PROXY_AUTH_TOKEN=替换为你的代理Token
AKSHARE_PROXY_RETRY=1
AKSHARE_HISTORY_CACHE_TTL_SECONDS=300
```

Bot 启动时会在第一次延迟导入 AKShare 前检查代理 Token 的积分，只有余额是
有效正数时才安装补丁。如果余额为零、格式无效或无法验证，补丁会保持关闭，
Bot 使用直连数据源，并通过已启用的通知渠道发送启动提醒；充值或修复 Token
后请重启 Bot。补丁只拦截本 Bot 使用的
东方财富域名；新浪备用行情和雪球基金类型检查仍直接请求。为避免放大付费
请求，补丁的并发 `fast` 分页始终关闭。请保持重试次数较低，绝不要把 Token
提交到 Git 或写入日志。启用后，付费东方财富请求只由代理补丁负责重试；Bot 对每个
东方财富 AKShare 操作只调用一次，避免两层重试相乘。
新浪、雪球等其他数据源仍使用普通的 `AKSHARE_RETRIES`。历史数据、实时行情和联接基金净值都使用短时进程内
缓存，相同或更窄的历史请求会复用结果，不建立可能长期过期的磁盘缓存。修改配置后要重启，
并查看启动日志；代理或数据源失败时 Bot 会安全跳过，不会猜测行情。

补丁的可选 `fast` 模式会并发请求通用的资金流/排行分页接口；本 Bot 使用的 ETF、
指数、股票和联接基金净值接口不需要这种分页，因此默认关闭。打开它可能增加并发
付费请求，却不会改善提醒任务。

## 技术栈

- Python 3.12
- python-telegram-bot
- SQLite
- AKShare 与 pandas
- APScheduler
- requests
- pytest 与 Ruff
- Docker 与 Docker Compose

明确不使用 Django、FastAPI、PostgreSQL、Redis、Celery、RSI 指标、Web UI
或自动交易功能。

## Docker 部署

生产镜像：

```text
ghcr.io/maxduke/fund-alert-bot:latest
```

测试或生产建议固定 `sha-<commit>` 标签，避免 `latest` 后续变化。完整的首次
安装、非 root UID/GID、升级、SQLite 备份和回滚注意事项见
[中文 VPS 部署指南](docs/deployment.zh-CN.md)。

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
ruff check .
pytest
```

Linux 使用 Docker Compose 时，必须在 `.env` 中把 `BOT_UID`、`BOT_GID`
设为非 root 用户的 `id -u`、`id -g`，并由同一用户先创建 `data`：

```bash
mkdir -p data
docker compose up -d
```

Compose 不会自动以 root 创建缺失的 `data`。已有部署切换 UID/GID 前必须先
停止容器，再修复整个 `data` 目录所有权；具体命令见部署指南。

不要提交 `.env` 或任何真实密钥。

## GitHub Actions

- `CI`：在 Python 3.12 上安装开发依赖，运行 Ruff 和 pytest。
- `Docker`：PR 构建镜像；`main` 推送成功后发布到
  `ghcr.io/maxduke/fund-alert-bot`。

## 项目文档

- [`docs/deployment.zh-CN.md`](docs/deployment.zh-CN.md)：VPS 部署与 SQLite 备份（[English](docs/deployment.md)）
- [`docs/investment-plan-guide.zh-CN.md`](docs/investment-plan-guide.zh-CN.md)：投资计划操作与边界（[English](docs/investment-plan-guide.md)）
- [`docs/architecture.md`](docs/architecture.md)：当前模块职责（开发者文档，英文）
- [`docs/investment-plan-implementation.md`](docs/investment-plan-implementation.md)：技术设计与验收清单（英文）
- [`docs/roadmap.md`](docs/roadmap.md)：历史实现阶段（英文）
- [`AGENTS.md`](AGENTS.md)：贡献者和编码代理边界（英文）
- [`.env.example`](.env.example)：只包含占位符的配置模板

## 范围边界

Bot 可以读取行情或基金数据、计算已支持的个人提醒条件、在 SQLite 保存状态、
调度检查并发送通知。

Bot 不得下单、交易、自动调仓、连接券商、提供投资建议、实现 RSI/RSI6、修改
`rsi6_monitor_bot`，或提供任何 Web 应用/API 服务。
