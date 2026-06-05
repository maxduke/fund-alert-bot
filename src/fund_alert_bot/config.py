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


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed runtime settings loaded from the environment."""

    sqlite_path: Path
    timezone: str
    telegram_bot_token: str
    telegram_allowed_user_ids: frozenset[int]


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
        timezone=os.environ.get("ALERT_TIMEZONE", DEFAULT_TIMEZONE),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_user_ids=parse_allowed_user_ids(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
        ),
    )
