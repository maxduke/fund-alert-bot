from pathlib import Path

from fund_alert_bot import __version__
from fund_alert_bot.commands import get_start_message
from fund_alert_bot.config import DEFAULT_SQLITE_PATH, load_settings
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
    assert "not implemented yet" in get_start_message()
