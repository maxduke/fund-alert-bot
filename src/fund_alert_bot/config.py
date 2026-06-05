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


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed runtime settings loaded from the environment."""

    sqlite_path: Path
    timezone: str


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
    )
