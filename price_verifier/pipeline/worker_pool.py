"""
worker_pool.py — concurrency primitives for the async pipeline.

A plain asyncio.Semaphore can't shrink once created, which is exactly what
the circuit breaker needs to do when failures spike ("back off globally...
reduce concurrency rather than hammering through", spec section 7).
DynamicGate replaces it with a capacity that can be resized mid-run.

PauseGate is the second primitive: a full stop-the-world pause (all workers
block before their next fetch) used for the circuit breaker's cooldown
window, distinct from a mere capacity reduction.
"""

from __future__ import annotations

import asyncio


class DynamicGate:
    def __init__(self, capacity: int):
        self._capacity = max(1, capacity)
        self._in_use = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self._in_use < self._capacity)
            self._in_use += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify_all()

    async def resize(self, new_capacity: int) -> None:
        async with self._cond:
            self._capacity = max(1, new_capacity)
            self._cond.notify_all()

    @property
    def capacity(self) -> int:
        return self._capacity


class PauseGate:
    """Normally open. `pause(seconds)` closes it for the duration, blocking
    every worker's `wait()` call until it reopens."""

    def __init__(self):
        self._event = asyncio.Event()
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    async def pause(self, seconds: float) -> None:
        self._event.clear()
        try:
            await asyncio.sleep(seconds)
        finally:
            self._event.set()
