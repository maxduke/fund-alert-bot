"""Environment-based application configuration."""

from __future__ import annotations

import math
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
DEFAULT_DCA_REMINDER_TIME = "09:30"
DEFAULT_AKSHARE_RETRIES = 3
DEFAULT_AKSHARE_RETRY_DELAY_SECONDS = 0.5
DEFAULT_AKSHARE_LATEST_LOOKBACK_DAYS = 45
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


def parse_positive_int_env(raw_value: str | None, *, name: str, default: int) -> int:
    """Parse a positive integer environment variable."""
    if raw_value is None or not raw_value.strip():
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc

    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def parse_non_negative_float_env(
    raw_value: str | None,
    *,
    name: str,
    default: float,
) -> float:
    """Parse a non-negative float environment variable."""
    if raw_value is None or not raw_value.strip():
        return default

    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc

    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


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
    dca_reminder_time: str
    telegram_bot_token: str
    telegram_allowed_user_ids: frozenset[int]
    akshare_retries: int
    akshare_retry_delay_seconds: float
    akshare_latest_lookback_days: int
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
        dca_reminder_time=os.environ.get(
            "DCA_REMINDER_TIME",
            DEFAULT_DCA_REMINDER_TIME,
        ),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_user_ids=parse_allowed_user_ids(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
        ),
        akshare_retries=parse_positive_int_env(
            os.environ.get("AKSHARE_RETRIES"),
            name="AKSHARE_RETRIES",
            default=DEFAULT_AKSHARE_RETRIES,
        ),
        akshare_retry_delay_seconds=parse_non_negative_float_env(
            os.environ.get("AKSHARE_RETRY_DELAY_SECONDS"),
            name="AKSHARE_RETRY_DELAY_SECONDS",
            default=DEFAULT_AKSHARE_RETRY_DELAY_SECONDS,
        ),
        akshare_latest_lookback_days=parse_positive_int_env(
            os.environ.get("AKSHARE_LATEST_LOOKBACK_DAYS"),
            name="AKSHARE_LATEST_LOOKBACK_DAYS",
            default=DEFAULT_AKSHARE_LATEST_LOOKBACK_DAYS,
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
