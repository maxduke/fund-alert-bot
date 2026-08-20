"""Run blocking market-data work without blocking the Telegram loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Lock


async def run_serialized[T](lock: Lock | None, work: Callable[[], T]) -> T:
    """Run one synchronous work unit in a worker thread.

    The production process shares one lock between command handlers and jobs so
    the stateful market provider/calendar remain single-threaded.  The lock is
    optional for direct callers and tests that already provide their own
    serialization.
    """

    def invoke() -> T:
        if lock is None:
            return work()
        # ponytail: one process-wide lock keeps provider state safe; split by
        # provider only if measured throughput requires concurrent requests.
        with lock:
            return work()

    return await asyncio.to_thread(invoke)
