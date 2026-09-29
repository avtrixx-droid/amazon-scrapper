"""
rate_limiter.py — AdaptiveRateLimiter: AIMD pacing for the fetch pipeline.

Three gates, all enforced by `await acquire()`:
  1. a concurrency slot (at most `max_concurrency` requests in flight);
  2. the next permitted start time — starts are spaced 1/rps apart with
     +/-`jitter` so the request stream isn't metronomic;
  3. any global pause set by a block event.

Feedback:
  - on_success(): additive increase — every `increase_every` consecutive
    successes, rps += increase_step (capped at max_rps).
  - on_block(): multiplicative decrease — rps *= 0.5 (floored at min_rps) and
    a global pause: base_pause for an isolated block, doubling for repeated
    blocks within `escalation_window` seconds, capped at max_pause. The
    escalation level steps back down after `decay_successes` successes.

Coalescing: when Amazon starts blocking, every request already in flight
comes back blocked at about the same time. Those are ONE block event, not
ten, so `acquire()` returns a ticket (the block epoch at start time) and
`on_block(ticket)` ignores reports from requests that started before the
latest registered block — they only get told how long the current pause
has left. Without a ticket, a report that arrives during an active pause is
coalesced the same way.

Time is injected (`clock`, `sleep`, `rng`) so tests are deterministic and
instantaneous. The limiter is asyncio-only (no threads): state reads and
writes between awaits are atomic by construction.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, Optional


class LimiterClosed(Exception):
    """Raised by acquire() once close() has been called."""


class AdaptiveRateLimiter:
    def __init__(
        self,
        initial_rps: float,
        min_rps: float,
        max_rps: float,
        max_concurrency: int,
        increase_step: float = 0.25,
        increase_every: int = 10,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        *,
        base_pause: float = 15.0,
        max_pause: float = 60.0,
        escalation_window: float = 120.0,
        decay_successes: int = 25,
        jitter: float = 0.25,
    ):
        if min_rps <= 0 or max_rps < min_rps:
            raise ValueError("require 0 < min_rps <= max_rps")
        self._min_rps = float(min_rps)
        self._max_rps = float(max_rps)
        self._rps = min(max(float(initial_rps), self._min_rps), self._max_rps)
        self._max_concurrency = max(1, int(max_concurrency))
        self._increase_step = float(increase_step)
        self._increase_every = max(1, int(increase_every))
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._base_pause = float(base_pause)
        self._max_pause = float(max_pause)
        self._escalation_window = float(escalation_window)
        self._decay_successes = max(1, int(decay_successes))
        self._jitter = max(0.0, min(float(jitter), 0.9))

        self._slots = asyncio.Semaphore(self._max_concurrency)
        self._closed = False
        self._closed_evt = asyncio.Event()
        self._in_flight = 0
        self._next_start = float("-inf")
        self._pause_until = float("-inf")

        self._epoch = 0                    # bumped once per registered block event
        self._level = 0                    # pause escalation level
        self._last_block_at: Optional[float] = None
        self._successes_since_increase = 0
        self._successes_since_block = 0
        self._block_streak = 0             # block events since the last success

        self._acquired = 0
        self._successes = 0
        self._block_reports = 0
        self._block_events = 0
        self._paused_seconds = 0.0
        self._lowest_rps = self._rps

    # ── gating ────────────────────────────────────────────────────────────
    async def acquire(self) -> int:
        """Wait for a slot, the pacing schedule, and any pause. Returns a
        ticket (the block epoch at start) to pass back to on_block()."""
        await self._slots.acquire()
        try:
            while True:
                if self._closed:
                    raise LimiterClosed()
                now = self._clock()
                if self._pause_until > now:
                    await self._nap(self._pause_until - now)
                    continue
                slot = max(now, self._next_start)
                # Reserve the slot before sleeping so concurrent waiters queue
                # up behind it instead of all waking for the same start time.
                spacing = (1.0 / self._rps) * (1.0 + self._jitter * (2.0 * self._rng() - 1.0))
                self._next_start = slot + spacing
                if slot > now:
                    await self._nap(slot - now)
                    if self._closed:
                        raise LimiterClosed()
                    if self._pause_until > self._clock():
                        continue  # a block landed while we waited — honour the pause
                break
        except BaseException:
            self._slots.release()
            raise
        self._in_flight += 1
        self._acquired += 1
        return self._epoch

    async def _nap(self, seconds: float) -> None:
        """The injected sleep, cut short by close()."""
        if self._closed:
            return
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        closer = asyncio.ensure_future(self._closed_evt.wait())
        try:
            await asyncio.wait({sleeper, closer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            closer.cancel()

    def close(self) -> None:
        """Stop admitting requests: every pending and future acquire() raises
        LimiterClosed promptly (waiters don't sit out their pacing slot).
        Used when a pass gives up so its queued rows drain immediately."""
        self._closed = True
        self._closed_evt.set()

    @property
    def closed(self) -> bool:
        return self._closed

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        self._slots.release()

    # ── feedback ──────────────────────────────────────────────────────────
    def on_success(self) -> None:
        self._successes += 1
        self._block_streak = 0
        self._successes_since_increase += 1
        if self._successes_since_increase >= self._increase_every:
            self._successes_since_increase = 0
            self._rps = min(self._max_rps, self._rps + self._increase_step)
        if self._level > 0:
            self._successes_since_block += 1
            if self._successes_since_block >= self._decay_successes:
                self._successes_since_block = 0
                self._level -= 1

    def on_block(self, ticket: Optional[int] = None) -> float:
        """Register a block/throttle response. Returns the pause (seconds)
        now in effect. A report from a request that started before the
        latest block event (stale ticket) — or, without a ticket, one that
        arrives during an active pause — is coalesced into that event: no
        further rate cut, no new pause, just the remaining pause returned."""
        self._block_reports += 1
        now = self._clock()
        stale = (ticket is not None and ticket < self._epoch) or (ticket is None and self._pause_until > now)
        if stale:
            return max(0.0, self._pause_until - now)

        if self._last_block_at is not None and now - self._last_block_at <= self._escalation_window:
            self._level += 1
        elif self._last_block_at is not None:
            self._level = 0
        pause = min(self._base_pause * (2 ** self._level), self._max_pause)
        # Don't let the level grow past what can still change the pause.
        while self._level > 0 and self._base_pause * (2 ** (self._level - 1)) >= self._max_pause:
            self._level -= 1

        self._epoch += 1
        self._block_events += 1
        self._block_streak += 1
        self._last_block_at = now
        self._successes_since_block = 0
        self._successes_since_increase = 0
        self._rps = max(self._min_rps, self._rps * 0.5)
        self._lowest_rps = min(self._lowest_rps, self._rps)
        self._pause_until = max(self._pause_until, now + pause)
        self._next_start = max(self._next_start, self._pause_until)
        self._paused_seconds += pause
        return pause

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def current_rps(self) -> float:
        return self._rps

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def block_streak(self) -> int:
        """Block events registered since the last success."""
        return self._block_streak

    def pause_remaining(self) -> float:
        return max(0.0, self._pause_until - self._clock())

    def stats(self) -> dict:
        return {
            "current_rps": round(self._rps, 3),
            "lowest_rps": round(self._lowest_rps, 3),
            "max_concurrency": self._max_concurrency,
            "in_flight": self._in_flight,
            "requests": self._acquired,
            "successes": self._successes,
            "block_reports": self._block_reports,
            "block_events": self._block_events,
            "block_streak": self._block_streak,
            "paused_seconds_total": round(self._paused_seconds, 3),
            "pause_remaining": round(self.pause_remaining(), 3),
            "escalation_level": self._level,
        }
