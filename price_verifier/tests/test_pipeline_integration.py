"""End-to-end pipeline tests against a simulated Amazon — no network, no
Chrome. The fake models what a live run showed: product pages parse fine,
but each identity (session) gets soft-blocked after a burst of requests and
NEVER recovers until the pipeline rotates to a new identity. Ambiguous pages
(in stock, no price) are routed to a fake real-browser fetcher.

All timings are tuned down (PipelineTuning) so the whole module runs in a
couple of seconds. Run:
  python -m unittest price_verifier.tests.test_pipeline_integration
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from price_verifier.fetcher.browser_fallback import BrowserUnavailable
from price_verifier.fetcher.models import FetchResult
from price_verifier.pipeline import runner
from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db

PRODUCT_HTML = """
<html><body>
<input type="hidden" id="ASIN" value="{asin}">
<div id="rightCol">
  <span id="productTitle">Product {asin}</span>
  {price_block}
  <div id="availability"><span>{availability}</span></div>
  {cta}
</div>
</body></html>
"""
PRICE_BLOCK = ('<div id="corePriceDisplay_desktop_feature_div"><span class="a-price apexPriceToPay">'
               '<span class="a-offscreen">{price}</span></span></div>')
CTA = '<input id="add-to-cart-button" name="submit.add-to-cart" type="submit">'
CAPTCHA_HTML = "<html><body><form action='/errors/validateCaptcha'><input id='captchacharacters'></form></body></html>"
NOT_FOUND_HTML = "<html><body>Looking for something? page-not-found</body></html>"


def product_html(asin: str, price: float | None = 1499.0, availability: str = "In stock.", cta: bool = True) -> str:
    return PRODUCT_HTML.format(
        asin=asin,
        price_block=PRICE_BLOCK.format(price=f"₹{price:,.2f}") if price is not None else "",
        availability=availability,
        cta=CTA if cta else "",
    )


def fast_tuning(**overrides) -> runner.PipelineTuning:
    t = runner.PipelineTuning(
        fast_initial_rps=400.0, fast_min_rps=100.0, fast_max_rps=800.0,
        rate_increase_step=10.0, rate_increase_every=5, rate_jitter=0.25,
        block_pause_base=0.01, block_pause_max=0.04, block_escalation_window=1.0, block_decay_successes=10,
        max_attempts_fast=2, fast_abort_block_streak=4, retry_backoff=0.001,
        recovery_pause=0.01, recovery_rps=400.0, recovery_concurrency=2, max_attempts_recovery=2,
        recovery_abort_block_streak=3,
        browser_gap_min=0.0, browser_gap_max=0.001, browser_block_pause=0.001, browser_abort_block_streak=3,
    )
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


class FakeAmazon:
    """Shared server-side state. `block_after` good responses per identity,
    then that identity only ever gets captcha pages. Identities numbered
    in `dead_identities` are blocked from their first request."""

    def __init__(self, *, block_after: int = 10**9, dead_identities=(), prices=None, ambiguous=(),
                 oos=(), not_found=(), latency: float = 0.001, network_down: bool = False):
        self.network_down = network_down
        self.block_after = block_after
        self.dead_identities = set(dead_identities)
        self.prices = dict(prices or {})
        self.ambiguous = set(ambiguous)
        self.oos = set(oos)
        self.not_found = set(not_found)
        self.latency = latency
        self.sessions: list[FakeSession] = []
        self.fetch_log: list[tuple[int, str]] = []

    def factory(self) -> "FakeSession":
        s = FakeSession(self, len(self.sessions) + 1)
        self.sessions.append(s)
        return s

    def page_for(self, asin: str) -> FetchResult:
        if asin in self.not_found:
            return FetchResult(asin=asin, status_code=404, html=NOT_FOUND_HTML)
        if asin in self.oos:
            return FetchResult(asin=asin, status_code=200,
                               html=product_html(asin, price=None, availability="Currently unavailable.", cta=False))
        if asin in self.ambiguous:
            return FetchResult(asin=asin, status_code=200, html=product_html(asin, price=None))
        return FetchResult(asin=asin, status_code=200, html=product_html(asin, self.prices.get(asin, 1499.0)))


class FakeSession:
    """FetchSession-like: start / fetch / rotate / close, never raises."""

    def __init__(self, amazon: FakeAmazon, ident: int):
        self.amazon = amazon
        self.ident = ident
        self.backend = "fake"
        self.started = False
        self.closed = False
        self.served = 0
        self.blocked = False
        self.fetches_after_block = 0
        self.fetches_after_close = 0

    async def start(self, warm_up: bool = True) -> None:
        self.started = True

    async def fetch(self, asin: str) -> FetchResult:
        if self.closed:
            self.fetches_after_close += 1
        if self.blocked:
            self.fetches_after_block += 1
        self.amazon.fetch_log.append((self.ident, asin))
        await asyncio.sleep(self.amazon.latency)
        if self.amazon.network_down:
            return FetchResult(asin=asin, status_code=None, html=None, error="connection_error")
        if self.ident in self.amazon.dead_identities or self.served >= self.amazon.block_after:
            self.blocked = True
            return FetchResult(asin=asin, status_code=200, html=CAPTCHA_HTML)
        self.served += 1
        return self.amazon.page_for(asin)

    async def rotate(self) -> None:  # pipeline rotates by replacing the session, but honour the contract
        self.served = 0
        self.blocked = False

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """BrowserFetcher-like (sync). Renders the price for ambiguous rows
    unless listed in `still_no_price`; `captcha_first` asins get one captcha
    before a restart; `always_blocked` makes every fetch a captcha."""

    def __init__(self, amazon: FakeAmazon, *, still_no_price=(), captcha_first=(), always_blocked=False,
                 fail_start=False, broken=False):
        self.broken = broken
        self.amazon = amazon
        self.still_no_price = set(still_no_price)
        self.captcha_first = set(captcha_first)
        self.always_blocked = always_blocked
        self.fail_start = fail_start
        self.starts = 0
        self.restarts = 0
        self.closed = 0
        self.fetched: list[str] = []

    def start(self) -> None:
        if self.fail_start:
            raise BrowserUnavailable("Google Chrome was not found.")
        self.starts += 1

    def restart(self) -> None:
        self.restarts += 1

    def close(self) -> None:
        self.closed += 1

    def fetch(self, asin: str, base_url=None) -> FetchResult:
        self.fetched.append(asin)
        if self.broken:
            return FetchResult(asin=asin, status_code=None, html=None, error="browser_error:WebDriverException",
                               source="browser")
        if self.always_blocked or (asin in self.captcha_first and self.fetched.count(asin) == 1):
            return FetchResult(asin=asin, status_code=None, html=CAPTCHA_HTML, source="browser")
        if asin in self.still_no_price:
            html = product_html(asin, price=None)
        elif asin in self.amazon.oos or asin in self.amazon.not_found:
            return FetchResult(asin=asin, status_code=None, html=self.amazon.page_for(asin).html, source="browser")
        else:
            html = product_html(asin, self.amazon.prices.get(asin, 1499.0))
        return FetchResult(asin=asin, status_code=None, html=html, source="browser")


def asins(n: int, prefix: str = "B0T") -> list[str]:
    return [f"{prefix}{i:07d}" for i in range(n)]


class PipelineHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Block events log at WARNING by design; expected here, so keep test output clean.
        self._log = logging.getLogger("price_verifier")
        self._log_level = self._log.level
        self._log.setLevel(logging.ERROR)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        init_db(self.db_path)
        self.outcomes: list[runner.ItemOutcome] = []
        self.phases: list[tuple[str, dict]] = []
        # Debug dumps are best-effort side output; keep them out of the repo during tests.
        self._dbg = mock.patch.object(runner, "_save_debug_html", lambda *a, **k: None)
        self._dbg.start()

    def tearDown(self):
        self._log.setLevel(self._log_level)
        self._dbg.stop()
        self.tmpdir.cleanup()

    def make_run(self, asin_list, expected=1499.0, brand="TestBrand"):
        items = [checkpoint.RunItemRow(asin=a, expected_price=expected, brand=brand) for a in asin_list]
        run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="t.csv"), items, db_path=self.db_path)
        return run_id, checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)

    async def on_item_done(self, outcome):
        self.outcomes.append(outcome)

    async def on_phase(self, phase, detail):
        self.phases.append((phase, detail))

    async def go(self, run_id, pending, amazon, browser=None, **kw):
        kw.setdefault("concurrency", 8)
        kw.setdefault("tuning", fast_tuning())
        return await runner.run_pipeline(
            run_id, pending, tolerance_abs=1.0, tolerance_pct=0.0, db_path=self.db_path,
            on_item_done=self.on_item_done, on_phase=self.on_phase,
            session_factory=amazon.factory,
            browser_factory=(lambda: browser) if browser is not None else None,
            **kw,
        )

    def rows(self, run_id):
        return {r["asin"]: r for r in checkpoint.get_run_items(run_id, db_path=self.db_path)}

    def assert_each_finalized_once(self, asin_list):
        counts = Counter(o.asin for o in self.outcomes)
        self.assertEqual(set(counts), set(asin_list))
        self.assertTrue(all(c == 1 for c in counts.values()), f"double finalize: {counts.most_common(3)}")


class SoftBlockRecoveryTests(PipelineHarness):
    async def test_soft_blocks_ambiguous_pages_and_terminal_states_end_with_zero_failures(self):
        all_asins = asins(60)
        ambiguous = set(all_asins[5:8])
        oos = {all_asins[10], all_asins[11]}
        not_found = {all_asins[12]}
        mismatched = {all_asins[20]: 1999.0}
        amazon = FakeAmazon(block_after=10, ambiguous=ambiguous, oos=oos, not_found=not_found, prices=mismatched)
        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(all_asins)

        mark_calls = Counter()
        real_mark = checkpoint.mark_item_result

        def spy_mark(*a, **k):
            mark_calls[k["asin"]] += 1
            return real_mark(*a, **k)

        t0 = time.monotonic()
        with mock.patch.object(checkpoint, "mark_item_result", spy_mark):
            stats = await self.go(run_id, pending, amazon, browser)
        elapsed = time.monotonic() - t0

        rows = self.rows(run_id)
        statuses = Counter(r["status"] for r in rows.values())
        self.assertEqual(statuses[checkpoint.STATUS_FAILED], 0, statuses)
        self.assertEqual(statuses[checkpoint.STATUS_PENDING], 0)
        self.assertEqual(statuses[checkpoint.STATUS_MATCHED], 60 - 2 - 1 - 1)
        self.assertEqual(statuses[checkpoint.STATUS_MISMATCHED], 1)
        self.assertEqual(statuses[checkpoint.STATUS_UNAVAILABLE], 2)
        self.assertEqual(statuses[checkpoint.STATUS_NOT_FOUND], 1)

        # exactly one final write + one callback per row
        self.assert_each_finalized_once(all_asins)
        self.assertEqual(set(mark_calls), set(all_asins))
        self.assertTrue(all(c == 1 for c in mark_calls.values()))

        # resolved_by always set; ambiguous rows were settled by the browser
        self.assertTrue(all(r["resolved_by"] in ("http", "recovery", "browser") for r in rows.values()))
        for a in ambiguous:
            self.assertEqual(rows[a]["resolved_by"], "browser")
            self.assertEqual(rows[a]["status"], checkpoint.STATUS_MATCHED)
            self.assertEqual(rows[a]["actual_price"], 1499.0)
        self.assertEqual(sorted(browser.fetched), sorted(ambiguous))
        self.assertEqual(browser.closed, 1)

        # blocks were detected, identities rotated, the rate backed off
        self.assertGreaterEqual(stats.fast.blocks, 1)
        self.assertGreaterEqual(stats.rotations, 1)
        self.assertLess(stats.fast.lowest_rps, fast_tuning().fast_initial_rps)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.finalized, 60)
        self.assertEqual(stats.pending, 0)
        self.assertEqual(sum(stats.resolved_by.values()), 60)
        self.assertEqual(stats.resolved_by.get("browser"), 3)
        self.assertEqual(stats.browser_available, True)

        # a blocked identity is never reused for new work: at most the
        # requests already in flight when the block landed hit it afterwards
        for s in amazon.sessions:
            self.assertLessEqual(s.fetches_after_block, 8, f"identity {s.ident} reused after block")
            self.assertEqual(s.fetches_after_close, 0)
        self.assertTrue(all(s.closed for s in amazon.sessions), "every identity closed at the end")

        # phases announced in order, ending with done + the stats dict
        names = [p for p, d in self.phases if d.get("event") == "start"]
        self.assertEqual(names[0], "fast")
        self.assertIn("browser", names)
        self.assertEqual(self.phases[-1][0], "done")
        self.assertEqual(self.phases[-1][1]["failed"], 0)
        self.assertTrue(any(d.get("event") == "block" and "pause_seconds" in d for _, d in self.phases))

        run = checkpoint.get_run(run_id, db_path=self.db_path)
        self.assertEqual(run["phase"], "done")
        self.assertEqual(run["failed"], 0)
        self.assertEqual(run["out_of_stock"], 3)
        self.assertEqual(checkpoint.get_run_stats(run_id, db_path=self.db_path)["finalized"], 60)

        self.assertLess(elapsed, 5.0, "tuned-down run should be fast")

    async def test_fast_pass_gives_up_on_a_dead_ip_and_recovery_pass_finishes(self):
        # First 6 identities (fast pass start + its rotations) are dead.
        all_asins = asins(30)
        amazon = FakeAmazon(dead_identities=range(1, 7))
        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(all_asins)

        stats = await self.go(run_id, pending, amazon, browser)

        rows = self.rows(run_id)
        self.assertTrue(stats.fast.aborted)
        self.assertLessEqual(stats.fast.blocks, fast_tuning().fast_abort_block_streak)
        self.assertEqual(stats.failed, 0)
        self.assertTrue(stats.recovery.ran)
        self.assertEqual({r["status"] for r in rows.values()}, {checkpoint.STATUS_MATCHED})
        by = Counter(r["resolved_by"] for r in rows.values())
        self.assertGreater(by["recovery"], 0)
        self.assert_each_finalized_once(all_asins)
        # an aborted fast pass drains immediately instead of burning attempts
        self.assertLessEqual(stats.fast.attempts, 30)

    async def test_everything_http_blocked_falls_back_to_browser(self):
        all_asins = asins(12)
        amazon = FakeAmazon(dead_identities=range(1, 100))
        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(all_asins)

        stats = await self.go(run_id, pending, amazon, browser)

        rows = self.rows(run_id)
        self.assertEqual({r["resolved_by"] for r in rows.values()}, {"browser"})
        self.assertEqual({r["status"] for r in rows.values()}, {checkpoint.STATUS_MATCHED})
        self.assertTrue(stats.fast.aborted and stats.recovery.aborted)
        self.assertEqual(stats.browser.resolved, 12)
        self.assert_each_finalized_once(all_asins)


class NetworkOutageTests(PipelineHarness):
    async def test_network_down_gives_up_quickly_everywhere_and_leaves_rows_retryable(self):
        all_asins = asins(200)
        amazon = FakeAmazon(network_down=True)
        browser = FakeBrowser(amazon, broken=True)
        run_id, pending = self.make_run(all_asins)
        t0 = time.monotonic()
        stats = await self.go(run_id, pending, amazon, browser)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertTrue(stats.fast.aborted and stats.recovery.aborted and stats.browser.aborted)
        t = fast_tuning()
        self.assertLess(stats.fast.attempts, t.fast_abort_error_streak + 20, "no per-row grind after abort")
        self.assertLess(stats.recovery.attempts, t.recovery_abort_error_streak + 5)
        self.assertLessEqual(len(browser.fetched), 2 * t.browser_abort_block_streak)
        self.assertEqual(stats.blocks, 0)
        self.assertEqual(stats.failed, 200)
        self.assert_each_finalized_once(all_asins)
        rows = self.rows(run_id)
        self.assertTrue(all("connection_error" in r["error_reason"] or "WebDriverException" in r["error_reason"]
                            for r in rows.values()), {r["error_reason"] for r in rows.values()})
        self.assertFalse(any("not attempted" in r["error_reason"] for r in rows.values()))
        self.assertEqual(len(checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)), 200)


class BrowserPassTests(PipelineHarness):
    async def test_ambiguous_page_still_without_price_in_chrome_finalizes_unavailable(self):
        a = asins(3)
        amazon = FakeAmazon(ambiguous={a[0]})
        browser = FakeBrowser(amazon, still_no_price={a[0]})
        run_id, pending = self.make_run(a)
        await self.go(run_id, pending, amazon, browser)
        row = self.rows(run_id)[a[0]]
        self.assertEqual(row["status"], checkpoint.STATUS_UNAVAILABLE)
        self.assertEqual(row["resolved_by"], "browser")
        self.assertIn("checked in Chrome", row["error_reason"])

    async def test_captcha_in_chrome_restarts_driver_and_retries_once(self):
        a = asins(2)
        amazon = FakeAmazon(ambiguous=set(a))
        browser = FakeBrowser(amazon, captcha_first={a[0]})
        run_id, pending = self.make_run(a)
        stats = await self.go(run_id, pending, amazon, browser)
        rows = self.rows(run_id)
        self.assertEqual(rows[a[0]]["status"], checkpoint.STATUS_MATCHED)
        self.assertEqual(browser.restarts, 1)
        self.assertEqual(stats.browser.rotations, 1)
        self.assertEqual(stats.browser.blocks, 1)

    async def test_chrome_blocked_too_stops_after_streak_and_leaves_rows_retryable(self):
        a = asins(6)
        amazon = FakeAmazon(dead_identities=range(1, 100))
        browser = FakeBrowser(amazon, always_blocked=True)
        run_id, pending = self.make_run(a)
        stats = await self.go(run_id, pending, amazon, browser)
        rows = self.rows(run_id)
        self.assertEqual({r["status"] for r in rows.values()}, {checkpoint.STATUS_FAILED})
        self.assertTrue(stats.browser.aborted)
        self.assertEqual(len(set(browser.fetched)), 3, "stops after browser_abort_block_streak rows")
        self.assertTrue(any("also blocking Chrome" in r["error_reason"] for r in rows.values()))
        self.assert_each_finalized_once(a)
        retry = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        self.assertEqual(sorted(r.asin for r in retry), sorted(a))

    async def test_chrome_cannot_start_marks_rows_failed_with_clear_reason(self):
        a = asins(3)
        amazon = FakeAmazon(ambiguous={a[1]}, dead_identities=())
        browser = FakeBrowser(amazon, fail_start=True)
        run_id, pending = self.make_run(a)
        stats = await self.go(run_id, pending, amazon, browser)
        row = self.rows(run_id)[a[1]]
        self.assertEqual(row["status"], checkpoint.STATUS_FAILED)
        self.assertIn("Google Chrome was not found", row["error_reason"])
        self.assertIn("in stock but no price", row["error_reason"])
        self.assertFalse(stats.browser_available)
        self.assertEqual(stats.status_counts[checkpoint.STATUS_MATCHED], 2)

    async def test_browser_fallback_disabled(self):
        a = asins(2)
        amazon = FakeAmazon(ambiguous={a[0]})
        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(a)
        await self.go(run_id, pending, amazon, browser, use_browser_fallback=False)
        row = self.rows(run_id)[a[0]]
        self.assertEqual(row["status"], checkpoint.STATUS_FAILED)
        self.assertIn("turned off", row["error_reason"])
        self.assertEqual(browser.fetched, [])

    async def test_chrome_not_installed_detected_without_factory(self):
        a = asins(1)
        amazon = FakeAmazon(ambiguous=set(a))
        run_id, pending = self.make_run(a)
        with mock.patch.object(runner.browser_fallback, "chrome_available", lambda: False):
            stats = await self.go(run_id, pending, amazon, None)
        row = self.rows(run_id)[a[0]]
        self.assertEqual(row["status"], checkpoint.STATUS_FAILED)
        self.assertIn("Chrome not found", row["error_reason"])
        self.assertFalse(stats.browser_available)


class CancelAndResumeTests(PipelineHarness):
    async def test_cancel_leaves_rest_pending_and_resume_completes(self):
        all_asins = asins(40)
        amazon = FakeAmazon(latency=0.005)
        run_id, pending = self.make_run(all_asins)
        cancel = asyncio.Event()

        async def on_done(outcome):
            self.outcomes.append(outcome)
            if len(self.outcomes) == 10:
                cancel.set()

        stats = await runner.run_pipeline(
            run_id, pending, concurrency=2, tolerance_abs=1.0, tolerance_pct=0.0, db_path=self.db_path,
            on_item_done=on_done, cancel_event=cancel, session_factory=amazon.factory,
            tuning=fast_tuning(fast_initial_rps=100.0, fast_max_rps=100.0),
        )
        self.assertTrue(stats.cancelled)
        done_first = {o.asin for o in self.outcomes}
        self.assertGreaterEqual(len(done_first), 10)
        self.assertLess(len(done_first), 40)
        rows = self.rows(run_id)
        pending_rows = [a for a, r in rows.items() if r["status"] == checkpoint.STATUS_PENDING]
        self.assertEqual(len(pending_rows) + len(done_first), 40, "every row is either final or still pending")
        self.assertEqual(checkpoint.get_run(run_id, db_path=self.db_path)["phase"], "cancelled")
        self.assertTrue(all(s.closed for s in amazon.sessions))

        # Resume picks up exactly the unfinished rows.
        resume = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        self.assertEqual(sorted(r.asin for r in resume), sorted(pending_rows))
        self.outcomes.clear()
        stats2 = await self.go(run_id, resume, amazon)
        self.assertFalse(stats2.cancelled)
        self.assertEqual({o.asin for o in self.outcomes}, set(pending_rows))
        self.assertEqual({r["status"] for r in self.rows(run_id).values()}, {checkpoint.STATUS_MATCHED})

    async def test_retry_failed_rows_via_reopen(self):
        a = asins(4)
        amazon = FakeAmazon(dead_identities=range(1, 100))
        run_id, pending = self.make_run(a)
        await self.go(run_id, pending, amazon, None, use_browser_fallback=False)
        self.assertEqual({r["status"] for r in self.rows(run_id).values()}, {checkpoint.STATUS_FAILED})
        checkpoint.finish_run(run_id, "/tmp/x.xlsx", db_path=self.db_path)

        # Network recovers; the "Retry failed" button re-runs only those rows.
        amazon.dead_identities = set()
        retry = checkpoint.reopen_run_for_retry(run_id, db_path=self.db_path)
        self.assertEqual(checkpoint.get_run(run_id, db_path=self.db_path)["status"], "running")
        self.outcomes.clear()
        await self.go(run_id, retry, amazon)
        self.assertEqual({r["status"] for r in self.rows(run_id).values()}, {checkpoint.STATUS_MATCHED})
        self.assertEqual(checkpoint.get_run(run_id, db_path=self.db_path)["failed"], 0)

    async def test_empty_items_is_a_noop(self):
        run_id, _ = self.make_run(asins(1))
        amazon = FakeAmazon()
        stats = await self.go(run_id, [], amazon)
        self.assertEqual(stats.total_items, 0)
        self.assertEqual(amazon.sessions, [])
        self.assertEqual(self.phases[-1][0], "done")


if __name__ == "__main__":
    unittest.main()


class SlowDiskTests(PipelineHarness):
    """Same scenarios with every DB write taking 40 ms, as on a slow Windows
    disk. Regression for two bugs that only showed up on the Windows CI
    runner: block/abort decisions made AFTER the (thread-hopped) DB writes
    let other workers keep hammering a flagged identity, and a freshly
    rotated identity that was also blocked was coalesced into the previous
    block event (rotation with no pause / no abort count)."""

    def setUp(self):
        super().setUp()
        real_db = runner._Pipeline._db

        async def slow_db(pipeline, fn, *a, **k):
            await asyncio.sleep(0.04)
            return await real_db(pipeline, fn, *a, **k)

        self._slow = mock.patch.object(runner._Pipeline, "_db", slow_db)
        self._slow.start()

    def tearDown(self):
        self._slow.stop()
        super().tearDown()

    async def test_dead_identities_still_count_as_block_events(self):
        amazon = FakeAmazon(dead_identities=range(1, 100))
        run_id, pending = self.make_run(asins(12))
        stats = await self.go(run_id, pending, amazon, FakeBrowser(amazon))
        self.assertTrue(stats.fast.aborted and stats.recovery.aborted)
        self.assertLessEqual(stats.fast.blocks, fast_tuning().fast_abort_block_streak)
        # every identity used by a pass is paid for with a counted block event
        self.assertLessEqual(stats.fast.rotations, stats.fast.blocks)
        self.assertEqual(stats.browser.resolved, 12)

    async def test_blocked_identity_not_reused_with_slow_writes(self):
        amazon = FakeAmazon(block_after=10)
        all_asins = asins(40)
        run_id, pending = self.make_run(all_asins)
        stats = await self.go(run_id, pending, amazon, FakeBrowser(amazon))
        self.assertEqual(stats.failed, 0)
        for s in amazon.sessions:
            self.assertLessEqual(s.fetches_after_block, 8, f"identity {s.ident} reused after block")
        self.assert_each_finalized_once(all_asins)

    async def test_network_down_batch_fails_quickly(self):
        amazon = FakeAmazon(network_down=True)
        run_id, pending = self.make_run(asins(200))
        t0 = time.monotonic()
        stats = await self.go(run_id, pending, amazon, FakeBrowser(amazon, broken=True))
        self.assertEqual(stats.failed, 200)
        self.assertLess(time.monotonic() - t0, 8.0, "remaining rows must be failed in one batch, not row by row")
