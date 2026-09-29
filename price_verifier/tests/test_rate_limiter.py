"""Deterministic AdaptiveRateLimiter tests — fake clock + fake sleep, so no
test waits on real time. Run:
  python -m unittest price_verifier.tests.test_rate_limiter
"""

from __future__ import annotations

import asyncio
import unittest

from price_verifier.pipeline.rate_limiter import AdaptiveRateLimiter, LimiterClosed


class FakeTime:
    """clock() reads virtual time; sleep(d) advances it by d."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.now += d
        await asyncio.sleep(0)


def make(ft: FakeTime, rng=lambda: 0.5, **kw) -> AdaptiveRateLimiter:
    params = dict(initial_rps=2.0, min_rps=0.5, max_rps=6.0, max_concurrency=4,
                  increase_step=0.5, increase_every=3)
    params.update(kw)
    return AdaptiveRateLimiter(clock=ft.clock, sleep=ft.sleep, rng=rng, **params)


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_starts_are_spaced_one_over_rps(self):
        ft = FakeTime()
        lim = make(ft)  # rng=0.5 -> zero jitter
        starts = []
        for _ in range(4):
            await lim.acquire()
            starts.append(ft.now)
            lim.release()
        self.assertEqual(starts, [0.0, 0.5, 1.0, 1.5])

    async def test_jitter_bounds(self):
        for rng_val, expected in ((0.0, 0.375), (1.0, 0.625)):  # 0.5 s spacing +/-25%
            ft = FakeTime()
            lim = make(ft, rng=lambda v=rng_val: v)
            await lim.acquire(); lim.release()
            await lim.acquire(); lim.release()
            self.assertAlmostEqual(ft.now, expected)

    async def test_concurrency_slots_bound_in_flight(self):
        ft = FakeTime()
        lim = make(ft, max_concurrency=2, initial_rps=1000, max_rps=1000)
        await lim.acquire()
        await lim.acquire()
        third = asyncio.ensure_future(lim.acquire())
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(third.done(), "third acquire must wait for a free slot")
        self.assertEqual(lim.stats()["in_flight"], 2)
        lim.release()
        await asyncio.wait_for(third, 1)
        self.assertEqual(lim.stats()["in_flight"], 2)

    async def test_cancelled_acquire_releases_its_slot(self):
        ft = FakeTime()
        gate = asyncio.Event()

        async def blocking_sleep(d):
            await gate.wait()

        lim = AdaptiveRateLimiter(2.0, 0.5, 6.0, 1, clock=ft.clock, sleep=blocking_sleep, rng=lambda: 0.5)
        await lim.acquire(); lim.release()
        waiter = asyncio.ensure_future(lim.acquire())  # must wait 0.5s for its pacing slot
        await asyncio.sleep(0.01)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        ft.now = 10.0
        await asyncio.wait_for(lim.acquire(), 1)  # slot was released; no deadlock


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_additive_increase_every_n_successes_capped(self):
        ft = FakeTime()
        lim = make(ft, initial_rps=5.0)
        for _ in range(2):
            lim.on_success()
        self.assertEqual(lim.current_rps, 5.0)
        lim.on_success()
        self.assertEqual(lim.current_rps, 5.5)
        for _ in range(9):
            lim.on_success()
        self.assertEqual(lim.current_rps, 6.0, "capped at max_rps")

    async def test_block_halves_rate_with_floor_and_pauses(self):
        ft = FakeTime()
        lim = make(ft, initial_rps=1.5)
        ticket = await lim.acquire(); lim.release()
        pause = lim.on_block(ticket)
        self.assertEqual(pause, 15.0)
        self.assertEqual(lim.current_rps, 0.75)
        await lim.acquire(); lim.release()
        self.assertGreaterEqual(ft.now, 15.0, "acquire must wait out the pause")
        t = await lim.acquire(); lim.release()
        lim.on_block(t)
        self.assertEqual(lim.current_rps, 0.5, "floored at min_rps")

    async def test_pause_escalates_within_window_and_resets_after(self):
        ft = FakeTime()
        lim = make(ft)
        pauses = []
        for _ in range(4):
            pauses.append(lim.on_block(lim.epoch))
            ft.now += 61  # past the pause, inside the 120 s window
        self.assertEqual(pauses, [15.0, 30.0, 60.0, 60.0])
        ft.now += 500  # outside the window
        self.assertEqual(lim.on_block(lim.epoch), 15.0)

    async def test_sustained_success_decays_escalation(self):
        ft = FakeTime()
        lim = make(ft, decay_successes=5)
        lim.on_block(lim.epoch); ft.now += 20
        lim.on_block(lim.epoch); ft.now += 40   # level 1 (30 s)
        for _ in range(5):
            lim.on_success()                    # decays back to level 0
        ft.now += 10
        self.assertEqual(lim.on_block(lim.epoch), 30.0, "level 0 -> repeat within window escalates to 30, not 60")

    async def test_concurrent_block_reports_coalesce_into_one_event(self):
        ft = FakeTime()
        lim = make(ft, initial_rps=4.0, max_concurrency=4)
        tickets = []
        for _ in range(3):
            tickets.append(await lim.acquire())
        for _ in range(3):
            lim.release()
        first = lim.on_block(tickets[0])
        ft.now += 2
        second = lim.on_block(tickets[1])
        third = lim.on_block(tickets[2])
        self.assertEqual(first, 15.0)
        self.assertEqual(second, 13.0, "stale ticket just gets the remaining pause")
        self.assertEqual(third, 13.0)
        self.assertEqual(lim.current_rps, 2.0, "halved exactly once")
        s = lim.stats()
        self.assertEqual(s["block_events"], 1)
        self.assertEqual(s["block_reports"], 3)

    async def test_ticketless_report_during_pause_is_coalesced(self):
        ft = FakeTime()
        lim = make(ft)
        lim.on_block()
        ft.now += 5
        self.assertEqual(lim.on_block(), 10.0)
        self.assertEqual(lim.stats()["block_events"], 1)

    async def test_block_streak_resets_on_success(self):
        ft = FakeTime()
        lim = make(ft)
        lim.on_block(lim.epoch)
        lim.on_block(lim.epoch)
        self.assertEqual(lim.block_streak, 2)
        lim.on_success()
        self.assertEqual(lim.block_streak, 0)

    async def test_close_interrupts_waiters_promptly(self):
        ft = FakeTime()
        never = asyncio.Event()

        async def forever(d):
            await never.wait()

        lim = AdaptiveRateLimiter(2.0, 0.5, 6.0, 2, clock=ft.clock, sleep=forever, rng=lambda: 0.5)
        lim.on_block()  # 15 s pause that the fake sleep would never finish
        waiters = [asyncio.ensure_future(lim.acquire()) for _ in range(3)]
        await asyncio.sleep(0.01)
        lim.close()
        results = await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), 1)
        self.assertTrue(all(isinstance(r, LimiterClosed) for r in results))
        with self.assertRaises(LimiterClosed):
            await lim.acquire()

    async def test_stats_shape(self):
        ft = FakeTime()
        lim = make(ft)
        await lim.acquire(); lim.release(); lim.on_success()
        s = lim.stats()
        for key in ("current_rps", "lowest_rps", "max_concurrency", "in_flight", "requests", "successes",
                    "block_reports", "block_events", "block_streak", "paused_seconds_total", "pause_remaining"):
            self.assertIn(key, s)
        self.assertEqual(s["requests"], 1)
        self.assertEqual(s["successes"], 1)

    def test_invalid_bounds_rejected(self):
        with self.assertRaises(ValueError):
            AdaptiveRateLimiter(1.0, 0.0, 2.0, 1)
        with self.assertRaises(ValueError):
            AdaptiveRateLimiter(1.0, 3.0, 2.0, 1)


if __name__ == "__main__":
    unittest.main()
