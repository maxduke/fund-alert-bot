from __future__ import annotations

import asyncio
import time
from threading import Lock

from fund_alert_bot.async_work import run_serialized


def test_run_serialized_keeps_loop_live_and_serializes_workers() -> None:
    async def scenario() -> tuple[int, int]:
        lock = Lock()
        active = 0
        max_active = 0

        def slow_work(value: int) -> int:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            time.sleep(0.06)
            active -= 1
            return value

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks += 1

        values = await asyncio.gather(
            run_serialized(lock, lambda: slow_work(1)),
            run_serialized(lock, lambda: slow_work(2)),
            ticker(),
        )
        assert values[:2] == [1, 2]
        return max_active, ticks

    max_active, ticks = asyncio.run(scenario())

    assert max_active == 1
    assert ticks == 5
