"""Environment-based application configuration."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
DEFAULT_BEFORE_CLOSE_CHECK_TIME = "14:50"
DEFAULT_DCA_REMINDER_TIME = "09:30"
DEFAULT_FUND_NAV_PROCESS_TIME = "08:30"
DEFAULT_BOT_LANGUAGE = "zh-CN"
DEFAULT_AKSHARE_RETRIES = 3
DEFAULT_AKSHARE_RETRY_DELAY_SECONDS = 0.5
DEFAULT_AKSHARE_LATEST_LOOKBACK_DAYS = 45
DEFAULT_AKSHARE_HISTORY_CACHE_TTL_SECONDS = 300.0
DEFAULT_AKSHARE_PROXY_RETRY = 1
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


def parse_bot_language(raw_value: str | None) -> str:
    """Parse the global user-facing language."""
    language = (raw_value or DEFAULT_BOT_LANGUAGE).strip()
    if language not in {"zh-CN", "en"}:
        raise ValueError("BOT_LANGUAGE must be one of: zh-CN, en")
    return language


def parse_hhmm_time(raw_value: str | None, *, name: str) -> str:
    """Parse a strict 24-hour ``HH:MM`` environment value."""
    value = (raw_value or "").strip()
    pieces = value.split(":")
    if len(pieces) != 2 or any(
        len(piece) != 2 or not piece.isdigit() for piece in pieces
    ):
        raise ValueError(f"{name} must use HH:MM")

    hour, minute = (int(piece) for piece in pieces)
    if hour > 23 or minute > 59:
        raise ValueError(f"{name} must be a valid 24-hour time")
    return time(hour=hour, minute=minute).strftime("%H:%M")


def parse_http_url(raw_value: str | None, *, name: str) -> str:
    """Normalize an optional URL and accept only absolute HTTP(S) URLs."""
    value = (raw_value or "").strip()
    if not value:
        return ""

    parsed = urlparse(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{name} must be an absolute http or https URL")
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
    before_close_check_time: str
    dca_reminder_time: str
    fund_nav_process_time: str
    bot_language: str
    telegram_bot_token: str
    telegram_allowed_user_ids: frozenset[int]
    akshare_retries: int
    akshare_retry_delay_seconds: float
    akshare_latest_lookback_days: int
    akshare_history_cache_ttl_seconds: float
    akshare_proxy_enabled: bool
    akshare_proxy_auth_token: str
    akshare_proxy_retry: int
    notifications: NotificationSettings


def load_settings(
    *,
    env_file: str | Path | None = None,
    load_env_file: bool = True,
) -> Settings:
    """Load settings from environment variables and an optional .env file."""
    if load_env_file:
        load_dotenv(dotenv_path=env_file)

    timezone = (os.environ.get("TZ") or DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("TZ must be a valid IANA timezone") from exc

    schedule_times = {
        "after_close_check_time": parse_hhmm_time(
            os.environ.get("AFTER_CLOSE_CHECK_TIME", DEFAULT_AFTER_CLOSE_CHECK_TIME),
            name="AFTER_CLOSE_CHECK_TIME",
        ),
        "before_close_check_time": parse_hhmm_time(
            os.environ.get(
                "BEFORE_CLOSE_CHECK_TIME",
                DEFAULT_BEFORE_CLOSE_CHECK_TIME,
            ),
            name="BEFORE_CLOSE_CHECK_TIME",
        ),
        "dca_reminder_time": parse_hhmm_time(
            os.environ.get("DCA_REMINDER_TIME", DEFAULT_DCA_REMINDER_TIME),
            name="DCA_REMINDER_TIME",
        ),
        "fund_nav_process_time": parse_hhmm_time(
            os.environ.get("FUND_NAV_PROCESS_TIME", DEFAULT_FUND_NAV_PROCESS_TIME),
            name="FUND_NAV_PROCESS_TIME",
        ),
    }

    akshare_proxy_enabled = parse_bool_env(
        os.environ.get("AKSHARE_PROXY_ENABLED"),
        name="AKSHARE_PROXY_ENABLED",
    )
    akshare_proxy_auth_token = os.environ.get("AKSHARE_PROXY_AUTH_TOKEN", "").strip()
    if akshare_proxy_enabled and not akshare_proxy_auth_token:
        raise ValueError(
            "AKSHARE_PROXY_AUTH_TOKEN is required when AKSHARE_PROXY_ENABLED=true"
        )

    bark_enabled = parse_bool_env(
        os.environ.get("BARK_ENABLED"),
        name="BARK_ENABLED",
    )
    bark_server_url = parse_http_url(
        os.environ.get("BARK_SERVER_URL"),
        name="BARK_SERVER_URL",
    )
    bark_device_key = os.environ.get("BARK_DEVICE_KEY", "").strip()
    if bark_enabled and (not bark_server_url or not bark_device_key):
        raise ValueError(
            "BARK_SERVER_URL and BARK_DEVICE_KEY are required when BARK_ENABLED=true"
        )

    ntfy_enabled = parse_bool_env(
        os.environ.get("NTFY_ENABLED"),
        name="NTFY_ENABLED",
    )
    ntfy_server_url = parse_http_url(
        os.environ.get("NTFY_SERVER_URL"),
        name="NTFY_SERVER_URL",
    )
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    if ntfy_enabled and (not ntfy_server_url or not ntfy_topic):
        raise ValueError(
            "NTFY_SERVER_URL and NTFY_TOPIC are required when NTFY_ENABLED=true"
        )

    webhook_enabled = parse_bool_env(
        os.environ.get("WEBHOOK_ENABLED"),
        name="WEBHOOK_ENABLED",
    )
    webhook_url = parse_http_url(
        os.environ.get("WEBHOOK_URL"),
        name="WEBHOOK_URL",
    )
    if webhook_enabled and not webhook_url:
        raise ValueError("WEBHOOK_URL is required when WEBHOOK_ENABLED=true")

    return Settings(
        sqlite_path=Path(
            os.environ.get("SQLITE_PATH", str(DEFAULT_SQLITE_PATH)).strip()
            or DEFAULT_SQLITE_PATH
        ),
        timezone=timezone,
        after_close_check_time=schedule_times["after_close_check_time"],
        before_close_check_time=schedule_times["before_close_check_time"],
        dca_reminder_time=schedule_times["dca_reminder_time"],
        fund_nav_process_time=schedule_times["fund_nav_process_time"],
        bot_language=parse_bot_language(os.environ.get("BOT_LANGUAGE")),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
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
        akshare_history_cache_ttl_seconds=parse_non_negative_float_env(
            os.environ.get("AKSHARE_HISTORY_CACHE_TTL_SECONDS"),
            name="AKSHARE_HISTORY_CACHE_TTL_SECONDS",
            default=DEFAULT_AKSHARE_HISTORY_CACHE_TTL_SECONDS,
        ),
        akshare_proxy_enabled=akshare_proxy_enabled,
        akshare_proxy_auth_token=akshare_proxy_auth_token,
        akshare_proxy_retry=parse_positive_int_env(
            os.environ.get("AKSHARE_PROXY_RETRY"),
            name="AKSHARE_PROXY_RETRY",
            default=DEFAULT_AKSHARE_PROXY_RETRY,
        ),
        notifications=NotificationSettings(
            bark_enabled=bark_enabled,
            bark_server_url=bark_server_url,
            bark_device_key=bark_device_key,
            ntfy_enabled=ntfy_enabled,
            ntfy_server_url=ntfy_server_url,
            ntfy_topic=ntfy_topic,
            webhook_enabled=webhook_enabled,
            webhook_url=webhook_url,
        ),
    )
