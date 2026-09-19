"""
circuit_breaker.py — trips on a run of consecutive fetch-attempt failures
(network errors, 429/503, CAPTCHA/blocked pages), independent of which
specific ASIN caused them. Mirrors scraper.py's proven circuit-breaker shape
("Failures grow with run size" fix in CLAUDE.md): escalating cooldown,
capped, plus a concurrency cut so the run doesn't keep hammering a session
Amazon has already started throttling.

Success (any terminal, correctly-parsed outcome — matched, mismatched, OOS,
not-found) resets the consecutive-failure counter. A row exhausting its own
retries and landing on FAILED does NOT separately trip the breaker beyond
the attempt-level failures that already fed it — the breaker only sees
attempts, not final row status.
"""

from __future__ import annotations

import asyncio

from price_verifier import config


class CircuitBreaker:
    def __init__(
        self,
        threshold: int = config.CIRCUIT_BREAKER_THRESHOLD,
        base_cooldown: float = config.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        max_cooldown: float = config.CIRCUIT_BREAKER_MAX_COOLDOWN_SECONDS,
    ):
        self._threshold = threshold
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._consecutive_failures = 0
        self._trips = 0
        self._lock = asyncio.Lock()

    async def record_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0

    async def record_failure(self) -> float | None:
        """Returns a cooldown duration in seconds if this failure tripped the
        breaker, else None. Also resets the consecutive counter on trip so
        the next window starts clean."""
        async with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._trips += 1
                self._consecutive_failures = 0
                cooldown = min(self._base_cooldown * self._trips, self._max_cooldown)
                return cooldown
            return None

    @property
    def trips(self) -> int:
        return self._trips
