"""Environment-based application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover

    def load_dotenv(
        dotenv_path: str | Path | None = None,
        *,
        override: bool = False,
    ) -> bool:
        """Fallback for smoke tests before dependencies are installed."""
        return False


DEFAULT_SQLITE_PATH = Path("/app/data/fund_alert_bot.sqlite3")
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_AFTER_CLOSE_CHECK_TIME = "17:10"
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off", ""})


def parse_allowed_user_ids(raw_value: str | None) -> frozenset[int]:
    """Parse a comma-separated Telegram user ID allowlist."""
    if raw_value is None or not raw_value.strip():
        return frozenset()

    allowed_user_ids: set[int] = set()
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            allowed_user_ids.add(int(item))
        except ValueError as exc:
            msg = "TELEGRAM_ALLOWED_USER_IDS must contain only integer user IDs"
            raise ValueError(msg) from exc

    return frozenset(allowed_user_ids)


def parse_bool_env(raw_value: str | None, *, name: str) -> bool:
    """Parse a boolean environment variable."""
    if raw_value is None:
        return False

    normalized = raw_value.strip().lower()
    if normalized in TRUE_ENV_VALUES:
        return True
    if normalized in FALSE_ENV_VALUES:
        return False

    msg = f"{name} must be one of: 1, true, yes, on, 0, false, no, off"
    raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    """Optional notification channel settings."""

    bark_enabled: bool = False
    bark_server_url: str = ""
    bark_device_key: str = ""
    ntfy_enabled: bool = False
    ntfy_server_url: str = ""
    ntfy_topic: str = ""
    webhook_enabled: bool = False
    webhook_url: str = ""


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed runtime settings loaded from the environment."""

    sqlite_path: Path
    timezone: str
    after_close_check_time: str
    telegram_bot_token: str
    telegram_allowed_user_ids: frozenset[int]
    notifications: NotificationSettings


def load_settings(
    *,
    env_file: str | Path | None = None,
    load_env_file: bool = True,
) -> Settings:
    """Load settings from environment variables and an optional .env file."""
    if load_env_file:
        load_dotenv(dotenv_path=env_file)

    return Settings(
        sqlite_path=Path(os.environ.get("SQLITE_PATH", str(DEFAULT_SQLITE_PATH))),
        timezone=os.environ.get("TZ", DEFAULT_TIMEZONE),
        after_close_check_time=os.environ.get(
            "AFTER_CLOSE_CHECK_TIME",
            DEFAULT_AFTER_CLOSE_CHECK_TIME,
        ),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_user_ids=parse_allowed_user_ids(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
        ),
        notifications=NotificationSettings(
            bark_enabled=parse_bool_env(
                os.environ.get("BARK_ENABLED"),
                name="BARK_ENABLED",
            ),
            bark_server_url=os.environ.get("BARK_SERVER_URL", ""),
            bark_device_key=os.environ.get("BARK_DEVICE_KEY", ""),
            ntfy_enabled=parse_bool_env(
                os.environ.get("NTFY_ENABLED"),
                name="NTFY_ENABLED",
            ),
            ntfy_server_url=os.environ.get("NTFY_SERVER_URL", ""),
            ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
            webhook_enabled=parse_bool_env(
                os.environ.get("WEBHOOK_ENABLED"),
                name="WEBHOOK_ENABLED",
            ),
            webhook_url=os.environ.get("WEBHOOK_URL", ""),
        ),
    )
