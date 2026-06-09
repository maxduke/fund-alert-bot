# VPS Deployment

This guide runs `fund-alert-bot` as a small Docker Compose service on a VPS.
The production compose file uses the published container image and stores local
SQLite data under the deployment directory.

`fund-alert-bot` is separate from `rsi6_monitor_bot`. Keep the two bots in
separate directories, use separate Telegram bot tokens, use separate data
volumes, and do not share a SQLite database between them.

Recommended layout:

```text
/opt/rsi6_monitor_bot
/opt/fund-alert-bot
```

## 1. Install Docker

On an Ubuntu VPS, install Docker Engine and the Compose plugin from Docker's
official packages:

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

Confirm Docker Compose is available:

```bash
docker compose version
```

## 2. Create The Deployment Directory

Create a dedicated directory for this bot:

```bash
sudo mkdir -p /opt/fund-alert-bot/data
sudo chown -R "$USER":"$USER" /opt/fund-alert-bot
cd /opt/fund-alert-bot
```

Copy `deploy/docker-compose.prod.yml` from this repository to:

```text
/opt/fund-alert-bot/docker-compose.yml
```

Copy `.env.example` from this repository to:

```text
/opt/fund-alert-bot/.env
```

## 3. Create `.env`

Edit `/opt/fund-alert-bot/.env` and replace placeholders with this bot's
settings:

```bash
nano /opt/fund-alert-bot/.env
```

Use a Telegram bot token dedicated to `fund-alert-bot`. Do not reuse the token
from `rsi6_monitor_bot`.

Minimum configuration:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-fund-alert-bot-token
TELEGRAM_ALLOWED_USER_IDS=
SQLITE_PATH=/app/data/fund_alert_bot.sqlite3
TZ=Asia/Shanghai
AFTER_CLOSE_CHECK_TIME=17:10
BEFORE_CLOSE_CHECK_TIME=14:50
DCA_REMINDER_TIME=09:30
```

Keep `.env` on the VPS only. Do not commit real secrets.

## 4. Log In To GHCR If Needed

If `ghcr.io/maxduke/fund-alert-bot:latest` is private, log in with a GitHub
token that has permission to read packages:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u maxduke --password-stdin
```

If the image is public, this step can be skipped.

## 5. Start The Bot

From `/opt/fund-alert-bot`, pull the image and start the service:

```bash
docker compose pull
docker compose up -d
```

Follow logs:

```bash
docker compose logs -f
```

Stop following logs with `Ctrl+C`; the container keeps running.

## Updating

Pull the latest image and recreate the container:

```bash
cd /opt/fund-alert-bot
docker compose pull
docker compose up -d
```

Check logs after updating:

```bash
docker compose logs -f
```

## Backing Up SQLite

The SQLite database lives at:

```text
/opt/fund-alert-bot/data/fund_alert_bot.sqlite3
```

For a simple consistent backup, stop the service, copy the database, then start
the service again:

```bash
cd /opt/fund-alert-bot
mkdir -p backups
docker compose stop
cp data/fund_alert_bot.sqlite3 "backups/fund_alert_bot-$(date +%F-%H%M%S).sqlite3"
docker compose up -d
```

The important file to copy is `data/fund_alert_bot.sqlite3`.

## Running Alongside `rsi6_monitor_bot`

Run the two bots as independent services:

```text
/opt/rsi6_monitor_bot
/opt/fund-alert-bot
```

Recommended separation:

- Use a different Telegram bot token for each bot.
- Use a different Compose project directory for each bot.
- Use a different data volume for each bot.
- Do not share a SQLite database.

Example:

```text
/opt/rsi6_monitor_bot/data
/opt/fund-alert-bot/data
```

This keeps RSI6 alerts owned by `rsi6_monitor_bot` and drawdown, DCA, and
profit-taking reminders owned by `fund-alert-bot`.
