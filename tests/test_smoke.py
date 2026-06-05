import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from fund_alert_bot import __version__
from fund_alert_bot.commands import (
    UNAUTHORIZED_MESSAGE,
    can_use_command,
    get_start_message,
    reject_if_unauthorized,
)
from fund_alert_bot.config import (
    DEFAULT_AFTER_CLOSE_CHECK_TIME,
    DEFAULT_SQLITE_PATH,
    DEFAULT_TIMEZONE,
    load_settings,
    parse_allowed_user_ids,
)
from fund_alert_bot.db import initialize_database, open_connection


def test_package_version_is_defined() -> None:
    assert __version__


def test_default_sqlite_path(monkeypatch) -> None:
    monkeypatch.delenv("SQLITE_PATH", raising=False)

    settings = load_settings(load_env_file=False)

    assert settings.sqlite_path == DEFAULT_SQLITE_PATH


def test_sqlite_path_from_environment(monkeypatch, tmp_path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"
    monkeypatch.setenv("SQLITE_PATH", str(sqlite_path))

    settings = load_settings(load_env_file=False)

    assert settings.sqlite_path == sqlite_path


def test_scheduler_defaults_from_environment(monkeypatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.delenv("AFTER_CLOSE_CHECK_TIME", raising=False)

    settings = load_settings(load_env_file=False)

    assert settings.timezone == DEFAULT_TIMEZONE
    assert settings.after_close_check_time == DEFAULT_AFTER_CLOSE_CHECK_TIME


def test_scheduler_settings_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    monkeypatch.setenv("AFTER_CLOSE_CHECK_TIME", "17:10")

    settings = load_settings(load_env_file=False)

    assert settings.timezone == "Asia/Shanghai"
    assert settings.after_close_check_time == "17:10"


def test_parse_allowed_user_ids_returns_empty_set_for_blank_values() -> None:
    assert parse_allowed_user_ids(None) == frozenset()
    assert parse_allowed_user_ids("  ") == frozenset()


def test_parse_allowed_user_ids_accepts_comma_separated_integers() -> None:
    assert parse_allowed_user_ids("123, 456,,123") == frozenset({123, 456})


def test_parse_allowed_user_ids_rejects_non_integer_values() -> None:
    with pytest.raises(
        ValueError,
        match="TELEGRAM_ALLOWED_USER_IDS must contain only integer user IDs",
    ):
        parse_allowed_user_ids("123,not-a-user")


def test_telegram_allowed_user_ids_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111,222")

    settings = load_settings(load_env_file=False)

    assert settings.telegram_allowed_user_ids == frozenset({111, 222})


def test_initialize_database_creates_metadata_table(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "fund_alert_bot.sqlite3"

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'app_metadata'
            """
        ).fetchone()

    assert sqlite_path.exists()
    assert row is not None


def test_placeholder_start_message() -> None:
    assert "/help" in get_start_message()


def test_permission_check_allows_only_configured_user_ids() -> None:
    allowed_update = SimpleNamespace(effective_user=SimpleNamespace(id=123))
    blocked_update = SimpleNamespace(effective_user=SimpleNamespace(id=456))
    anonymous_update = SimpleNamespace(effective_user=None)

    assert can_use_command(allowed_update, {123})
    assert not can_use_command(blocked_update, {123})
    assert not can_use_command(anonymous_update, {123})


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def test_empty_allowed_user_ids_rejects_and_logs_warning(caplog) -> None:
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_message=message,
    )
    caplog.set_level(logging.WARNING, logger="fund_alert_bot.commands")

    rejected = asyncio.run(reject_if_unauthorized(update, frozenset()))

    assert rejected
    assert message.replies == [UNAUTHORIZED_MESSAGE]
    assert "TELEGRAM_ALLOWED_USER_IDS is empty" in caplog.text
