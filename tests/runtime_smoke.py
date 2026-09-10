"""Offline smoke check of installed dependencies and the real polling lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import akshare
import curl_cffi
import pandas as pd
from py_mini_racer import py_mini_racer
from telegram.ext import Application, ApplicationBuilder
from telegram.request import BaseRequest

from fund_alert_bot.commands import create_application, publish_bot_command_menu
from fund_alert_bot.db import initialize_database, open_connection
from fund_alert_bot.scheduler import create_scheduler


class OfflineRequest(BaseRequest):
    @property
    def read_timeout(self) -> float:
        return 1.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url, method, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "getMe":
            result = {"id": 123, "is_bot": True, "first_name": "Offline smoke"}
        elif endpoint in {"setMyCommands", "deleteWebhook"}:
            result = True
        elif endpoint == "getUpdates":
            await asyncio.sleep(0.01)
            result = []
        else:
            raise AssertionError(f"Unexpected Telegram method: {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


def smoke() -> None:
    assert akshare.__version__
    assert curl_cffi.__version__
    assert pd.Series([1, 2, 3]).sum() == 6
    engine = py_mini_racer.MiniRacer()
    assert engine.eval("1 + 1") == 2
    events = []
    scheduler = create_scheduler(timezone="Asia/Shanghai")

    async def start(application):
        await publish_bot_command_menu(application)

        async def stop_when_running():
            if application.running:
                scheduler.remove_job("stop-when-running")
                events.append("job")
                application.stop_running()

        scheduler.add_job(
            stop_when_running, "interval", seconds=0.01, id="stop-when-running"
        )
        scheduler.start()
        events.append("started")

    async def shutdown(application):
        scheduler.shutdown(wait=True)
        await asyncio.sleep(0)
        events.append("stopped")

    with TemporaryDirectory(prefix="fund-alert-runtime-") as temporary:
        sqlite_path = Path(temporary) / "smoke.sqlite3"
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
        builder = (
            ApplicationBuilder()
            .request(OfflineRequest())
            .get_updates_request(OfflineRequest())
        )
        with patch.object(Application, "builder", return_value=builder):
            application = create_application(
                token="123:offline-test-token",
                allowed_user_ids={123},
                sqlite_path=sqlite_path,
                market_data_provider=object(),
                market_calendar=object(),
                post_init=start,
                post_shutdown=shutdown,
            )
        # Python 3.14 no longer implicitly creates a loop here. Test actual PTB
        # initialization instead of replacing run_polling with a test double.
        asyncio.set_event_loop(None)
        try:
            application.run_polling(stop_signals=None)
        finally:
            asyncio.set_event_loop(None)
    assert events == ["started", "job", "stopped"], events
    assert not scheduler.running
    print(
        json.dumps(
            {"python": sys.version, "akshare": akshare.__version__, "events": events}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-non-root", action="store_true")
    args = parser.parse_args()
    if args.require_non_root:
        assert os.getuid() != 0
    faulthandler.dump_traceback_later(20, exit=True)
    try:
        smoke()
    finally:
        faulthandler.cancel_dump_traceback_later()
