# VPS 部署

[English](deployment.md) | [简体中文](deployment.zh-CN.md)

本指南使用 Docker Compose 在 VPS 上运行一个小型 `fund-alert-bot` 服务。
生产 Compose 文件使用已发布的容器镜像，并把 SQLite 数据保存在部署目录中。

`fund-alert-bot` 与 `rsi6_monitor_bot` 是两个独立项目。两者必须使用不同目录、
不同 Telegram Bot Token、不同数据卷，并且绝不能共享 SQLite 数据库。

推荐目录：

```text
/opt/rsi6_monitor_bot
/opt/fund-alert-bot
```

## 1. 安装 Docker

在 Ubuntu VPS 上，从 Docker 官方软件源安装 Docker Engine 和 Compose 插件：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

确认 Compose 可用：

```bash
docker compose version
```

## 2. 创建部署目录

为本 Bot 创建独立目录：

```bash
sudo mkdir -p /opt/fund-alert-bot/data
sudo chown -R "$USER":"$USER" /opt/fund-alert-bot
cd /opt/fund-alert-bot
chmod 700 data
```

把仓库中的 `deploy/docker-compose.prod.yml` 复制为：

```text
/opt/fund-alert-bot/docker-compose.yml
```

把仓库中的 `.env.example` 复制为：

```text
/opt/fund-alert-bot/.env
```

使用专门的非 root 部署账号，并记录部署目录所有者的 UID/GID：

```bash
(
set -eu
BOT_OWNER_UID="$(id -u)"
BOT_OWNER_GID="$(id -g)"
if [ "$BOT_OWNER_UID" -eq 0 ] || [ "$BOT_OWNER_GID" -eq 0 ]; then
  echo "Use a non-root deployment account." >&2
  exit 1
fi
printf 'BOT_UID=%s\nBOT_GID=%s\n' "$BOT_OWNER_UID" "$BOT_OWNER_GID"
)
```

如果检查失败，立即停止并切换到非 root 账号；绝不能把 Bot 配置为 `0:0`。

## 3. 创建 `.env`

编辑 `/opt/fund-alert-bot/.env`，替换所有占位符：

```bash
nano /opt/fund-alert-bot/.env
chmod 600 /opt/fund-alert-bot/.env
```

使用 `fund-alert-bot` 专用 Telegram Bot Token，不要复用
`rsi6_monitor_bot` 的 Token。

最小配置：

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-fund-alert-bot-token
TELEGRAM_ALLOWED_USER_IDS=123456789
BOT_LANGUAGE=zh-CN
BOT_UID=1000
BOT_GID=1000
SQLITE_PATH=/app/data/fund_alert_bot.sqlite3
TZ=Asia/Shanghai
AFTER_CLOSE_CHECK_TIME=17:10
BEFORE_CLOSE_CHECK_TIME=14:50
DCA_REMINDER_TIME=09:30
FUND_NAV_PROCESS_TIME=08:30
```

必须把 `123456789` 替换为至少一个获准使用的 Telegram 数字用户 ID；多个 ID
使用英文逗号分隔。命令仅允许在私聊中使用。只有启用了 Bark、ntfy 或 webhook
时才能留空 allowlist；否则进程会拒绝启动，避免提醒任务在没有接收者时静默运行。

`BOT_LANGUAGE` 控制全部用户可见回复、按钮和通知渠道，仅支持 `zh-CN` 与
`en`。修改后重启容器；Telegram 命令名称仍使用英文。

把 `BOT_UID`、`BOT_GID` 替换为 `id -u`、`id -g` 实际输出。Compose 用这
两个值以非 root 身份运行 Bot，并写入宿主机所有的 `data` 目录。值不匹配会
导致 SQLite 无法打开；不要用全员可写权限绕过问题。

`.env` 只保存在 VPS，不得提交真实密钥。

### 可选的付费东方财富代理

没有有效的付费 Token 时保持关闭。如果东方财富开始限流，把下面配置加入 VPS
上的 `.env` 后重启：

```dotenv
AKSHARE_PROXY_ENABLED=true
AKSHARE_PROXY_AUTH_TOKEN=你的付费Token
AKSHARE_PROXY_RETRY=1
AKSHARE_HISTORY_CACHE_TTL_SECONDS=300
```

Bot 启动时会在第一次延迟导入 AKShare 前检查 Token 积分，只有有效正余额才会
安装补丁。余额为零、格式无效或无法验证时，补丁保持关闭，Bot 使用直连数据源，
并通过已启用的通知渠道发送提醒；充值或修复 Token 后请重启容器。容器已经包含固定版本
[`akshare-proxy-patch==0.5.0`](https://github.com/HelloYie/akshare-proxy-patch)，运行时不需要再安装。
Bot 只代理自身使用的东方财富域名，并始终关闭插件的并发 `fast` 模式。
代理服务目前只提供 HTTP 积分查询接口，设置 `AKSHARE_PROXY_ENABLED=true`
会把可复用的 Token 放在未加密的 URL 中，网络中间节点以及服务商访问日志都可能
看到它。只有在你明确接受该风险并信任网络路径时才启用；如果服务商提供支持，
请优先改用 HTTPS 且通过请求头或请求体传递凭据的接口。
较低的重试次数和短时进程内缓存是控制付费请求的有意设置。启用代理后，东方财富重试
由代理补丁统一负责，Bot 对每个东方财富操作只调用一次；其他数据源仍使用普通
重试预算。不要把 Token 放进 Compose 文件、Shell 历史、日志或 Git。代理和备用
数据源都不可用时，Bot 会发送“数据
不可用”提醒，不会消耗回撤档位，也不会伪造价格。ETF、指数和股票的实时行情都使用有边界的单品种请求，Bot 不调用 AKShare 全市场分页行情接口。

## 4. 必要时登录 GHCR

生产 Compose 要求在 `BOT_IMAGE_TAG` 中显式填写不可变镜像标签，使用 GitHub
Actions 发布的完整 commit SHA，例如：

```dotenv
BOT_IMAGE_TAG=sha-0123456789abcdef0123456789abcdef01234567
```

如果镜像是私有的，使用有 package 读取权限的 GitHub Token 登录：

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u maxduke --password-stdin
```

公开镜像可跳过此步骤。

## 5. 启动 Bot

在 `/opt/fund-alert-bot` 中拉取镜像并启动：

```bash
grep -Eq '^BOT_IMAGE_TAG=sha-[0-9a-f]{40}$' .env
docker compose config >/dev/null
docker compose pull
docker compose up -d
```

依赖定时提醒前，先验证配置身份能写数据目录：

```bash
docker compose run --rm --entrypoint sh fund-alert-bot -c 'test -w /app/data'
```

镜像健康检查要求调度器至少每分钟更新一次
`data/fund_alert_bot.sqlite3.heartbeat`。依赖提醒前确认容器健康：

```bash
docker compose ps
```

心跳文件超过三分钟未更新时，健康检查会失败。

查看日志：

```bash
docker compose logs -f
```

按 `Ctrl+C` 只会退出日志跟踪，容器继续运行。

## 更新

第一次升级到使用 `BOT_UID`、`BOT_GID` 的版本时，必须先把真实值写入
`.env`，安装并校验当前生产 Compose 文件，停止旧容器，再迁移现有数据库
目录所有权：

```bash
(
set -eu
cd /opt/fund-alert-bot
BOT_OWNER_UID="$(id -u)"
BOT_OWNER_GID="$(id -g)"
if [ "$BOT_OWNER_UID" -eq 0 ] || [ "$BOT_OWNER_GID" -eq 0 ]; then
  echo "Use a non-root deployment account." >&2
  exit 1
fi
printf 'BOT_UID=%s\nBOT_GID=%s\n' "$BOT_OWNER_UID" "$BOT_OWNER_GID"
nano .env
curl -fsSL \
  https://raw.githubusercontent.com/maxduke/fund-alert-bot/main/deploy/docker-compose.prod.yml \
  -o docker-compose.yml.new
docker compose -f docker-compose.yml.new config >/dev/null
mv docker-compose.yml.new docker-compose.yml
docker compose stop
sudo chown -R "$BOT_OWNER_UID:$BOT_OWNER_GID" data
)
```

下载和校验步骤必须在改变所有权前完成，确保安装的新 Compose 已包含 `user`
配置。不要用缺少该配置的旧 Compose 执行所有权迁移。

生产 Compose 在 `BOT_UID` 或 `BOT_GID` 缺失时会拒绝启动，防止升级后悄悄
把现有 SQLite 变成不可写。

编辑 `.env`，将 `BOT_IMAGE_TAG` 改为新发布的 `sha-<完整 commit>` 标签；保留
旧值，以便健康检查失败时回滚。然后拉取固定版本并重建容器：

```bash
grep -Eq '^BOT_IMAGE_TAG=sha-[0-9a-f]{40}$' .env
docker compose config >/dev/null
docker compose pull
docker compose up -d
docker compose run --rm --entrypoint sh fund-alert-bot -c 'test -w /app/data'
```

更新后检查日志：

```bash
docker compose logs -f
```

## 备份 SQLite

数据库位置：

```text
/opt/fund-alert-bot/data/fund_alert_bot.sqlite3
```

最简单的一致性备份方式是停止服务、复制数据库、再启动：

```bash
set -Eeuo pipefail
cd /opt/fund-alert-bot
mkdir -p backups
chmod 700 backups
umask 077
backup="backups/fund_alert_bot-$(date +%F-%H%M%S).sqlite3"
restart=1
restart_service() {
  status=$?
  if [ "$restart" -eq 1 ] && ! docker compose up -d; then
    echo "无法重启 fund-alert-bot，请立即检查 Docker。" >&2
    status=1
  fi
  exit "$status"
}
trap restart_service EXIT
docker compose stop
cp data/fund_alert_bot.sqlite3 "$backup"
test "$(sqlite3 "$backup" 'PRAGMA integrity_check;')" = "ok"
age -r "$AGE_RECIPIENT" -o "$backup.age" "$backup"
test -s "$backup.age"
rclone copyto "$backup.age" "remote:fund-alert-bot-backups/$(basename "$backup.age")"
rclone check "$backup.age" \
  "remote:fund-alert-bot-backups/$(basename "$backup.age")" --download
rm -f "$backup"
docker compose up -d
restart=0
```

任何步骤失败时，退出陷阱都会尝试重启服务。加密或上传失败时会保留明文副本供
排查；只有在修复失败原因并确认加密备份安全后，才手动删除明文副本。

建议通过定时任务至少每天运行一次。`AGE_RECIPIENT` 是公开接收者，私钥必须保存
在 VPS 之外（例如离线密码管理器）；不得放入仓库、Compose、Shell 历史或日志。
上面的 `remote:` 是通过 `rclone` 配置的加密异地存储示例。

至少每月在另一台机器或独立目录做一次恢复演练：

```bash
mkdir -m 700 restore-drill
rclone copy remote:fund-alert-bot-backups/ restore-drill/
age -d -i /secure/offline-age-identity.txt \
  -o restore-drill/fund_alert_bot.sqlite3 \
  restore-drill/fund_alert_bot-YYYY-MM-DD-HHMMSS.sqlite3.age
sqlite3 restore-drill/fund_alert_bot.sqlite3 'PRAGMA integrity_check;'
```

只有 SQLite 输出 `ok` 才算恢复成功，并记录演练日期和所用备份。应用镜像回滚
时，把 `.env` 中的 `BOT_IMAGE_TAG` 恢复为旧值，再执行
`docker compose pull && docker compose up -d` 并检查日志。

## 与 `rsi6_monitor_bot` 同机运行

两个 Bot 作为独立服务运行：

```text
/opt/rsi6_monitor_bot
/opt/fund-alert-bot
```

必须分开：

- Telegram Bot Token
- Compose 项目目录
- 数据卷或绑定目录
- SQLite 数据库

例如：

```text
/opt/rsi6_monitor_bot/data
/opt/fund-alert-bot/data
```

这样 RSI6 提醒仍由 `rsi6_monitor_bot` 管理；回撤、定投、Price-Gain 与通知
投递状态由 `fund-alert-bot` 管理。
