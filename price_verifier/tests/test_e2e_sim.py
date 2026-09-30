"""
test_e2e_sim.py — the whole pipeline, with the production FetchSession
(curl_cffi), against sim_amazon.SimAmazon: a local fake amazon.in that
flags an identity after a short burst and 503s it from then on, exactly the
failure mode of the vendor's first live run (30 ASINs, ~10 min, most rows
failed).

What these prove, end to end over real HTTP:
  * every row gets a definitive answer — zero FAILED rows — even when the
    server blocks identities mid-run (rotation + pacing + recovery pass),
  * prices / MRP / seller / brand are read correctly off realistic pages
    (carousel prices, strike-through MRPs and review text all present as
    decoys),
  * unavailable / no-buy-box / not-found pages land in their own statuses,
  * the run is fast: a 40-ASIN run finishes in well under a minute.

The Chrome pass is disabled (no browser in CI); the point is that the HTTP
passes alone must already get to zero failures under this throttle model.

Set PV_SIM_BENCH=1 to also run a 300-ASIN benchmark with the production
tuning (config.py values, including real pauses) and print the timing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from price_verifier.fetcher.http_client import FetchSession
from price_verifier.pipeline import runner
from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db
from price_verifier.tests.sim_amazon import SimAmazon, ThrottlePolicy, make_catalog

KINDS = {"unavailable": 0.08, "no_offer": 0.08, "not_found": 0.05}
EXPECTED_STATUS = {
    "in_stock": None,  # matched or mismatched, depending on the expected price
    "unavailable": checkpoint.STATUS_UNAVAILABLE,
    "no_offer": checkpoint.STATUS_NO_FEATURED_OFFER,
    "not_found": checkpoint.STATUS_NOT_FOUND,
}


def quick_tuning() -> runner.PipelineTuning:
    """The production pacing SHAPE (same AIMD rules, same relative steps)
    at 3x the production rates, with the long pauses shortened, so a test
    run takes seconds instead of the minutes the deliberately patient
    production defaults would. The simulator's throttle is unchanged, so
    blocks and rotations still happen. The PV_SIM_BENCH benchmark below runs
    the exact production tuning."""
    t = runner.PipelineTuning.from_config()
    for name in ("fast_initial_rps", "fast_min_rps", "fast_max_rps", "rate_increase_step", "recovery_rps"):
        setattr(t, name, getattr(t, name) * 3)
    t.block_pause_base = 1.0
    t.block_pause_max = 3.0
    t.recovery_pause = 1.0
    t.retry_backoff = 0.2
    return t


class E2ESimBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "e2e.db"
        init_db(self.db_path)
        self._dbg = mock.patch.object(runner, "_save_debug_html", lambda *a, **k: None)
        self._dbg.start()
        logging.getLogger("price_verifier").setLevel(logging.ERROR)
        self.sim = None

    def tearDown(self):
        if self.sim is not None:
            self.sim.stop()
        logging.getLogger("price_verifier").setLevel(logging.NOTSET)
        self._dbg.stop()
        self.tmpdir.cleanup()

    def run_sim(self, n: int, policy: ThrottlePolicy, tuning: runner.PipelineTuning, concurrency: int = 15,
                seed: int = 7):
        catalog = make_catalog(n, seed=seed, kinds=KINDS)
        self.sim = SimAmazon(catalog, policy, padding_kb=150).start()
        base_url = self.sim.base_url

        # Every third in-stock row carries a wrong expected price.
        items, self.expect_mismatch = [], set()
        for i, p in enumerate(catalog.values()):
            expected = p.price if p.price is not None else 100.0
            if p.kind == "in_stock" and i % 3 == 0:
                expected = p.price + 25.0
                self.expect_mismatch.add(p.asin)
            items.append(checkpoint.RunItemRow(asin=p.asin, expected_price=expected, brand=""))
        run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="sim.csv"), items, db_path=self.db_path)
        pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)

        sessions: list[FetchSession] = []

        def factory():
            s = FetchSession(base_url=base_url)
            sessions.append(s)
            return s

        t0 = time.monotonic()
        stats = asyncio.run(runner.run_pipeline(
            run_id, pending, concurrency=concurrency, tolerance_abs=1.0, tolerance_pct=0.0,
            use_browser_fallback=False, db_path=self.db_path, session_factory=factory, tuning=tuning,
        ))
        elapsed = time.monotonic() - t0
        rows = {r["asin"]: r for r in checkpoint.get_run_items(run_id, db_path=self.db_path)}
        self.assertTrue(sessions, "the pipeline never built a session")
        self.assertTrue(all(s.backend == "curl_cffi" for s in sessions),
                        f"expected the production curl_cffi backend, got {[s.backend for s in sessions]}")
        return catalog, rows, stats, elapsed

    def assert_all_correct(self, catalog, rows):
        failed = {a: r["error_reason"] for a, r in rows.items() if r["status"] == checkpoint.STATUS_FAILED}
        self.assertEqual(failed, {}, "rows ended FAILED")
        self.assertFalse([a for a, r in rows.items() if r["status"] == checkpoint.STATUS_PENDING], "rows left pending")
        for asin, p in catalog.items():
            r = rows[asin]
            want = EXPECTED_STATUS[p.kind]
            if want is None:
                want = checkpoint.STATUS_MISMATCHED if asin in self.expect_mismatch else checkpoint.STATUS_MATCHED
                self.assertEqual(r["actual_price"], p.price, asin)
                self.assertEqual(r["mrp"], p.mrp, asin)
                self.assertEqual(r["seller"], p.seller, asin)
                self.assertEqual(r["effective_brand"], p.brand, asin)
            self.assertEqual(r["status"], want, f"{asin} ({p.kind})")


class E2ESimTests(E2ESimBase):
    def test_realistic_throttle_zero_failures_and_fast(self):
        """Throttle model tuned to what the live run showed: burst, then a
        per-identity sustained rate, plus a per-IP ceiling."""
        catalog, rows, stats, elapsed = self.run_sim(40, ThrottlePolicy(), quick_tuning())
        self.assert_all_correct(catalog, rows)
        # The vendor's run took ~10 min for 30 ASINs; this must be a small
        # fraction of that even with production request rates.
        self.assertLess(elapsed, 60.0, f"40 ASINs took {elapsed:.1f}s")

    def test_harsh_throttle_still_zero_failures(self):
        """A much stricter site: tiny bursts, slow sustained rate, low IP
        ceiling. Identities get flagged repeatedly; rotation + AIMD pacing +
        the recovery pass must still resolve every row over HTTP."""
        policy = ThrottlePolicy(burst=5, sustained_rps=1.0, anon_burst=2, ip_ceiling_rps=3.0)
        catalog, rows, stats, elapsed = self.run_sim(30, policy, quick_tuning(), seed=11)
        self.assert_all_correct(catalog, rows)
        self.assertGreater(stats.fast.blocks + stats.recovery.blocks, 0,
                           "harsh policy should have produced at least one block (test is not exercising rotation)")
        self.assertLess(elapsed, 120.0, f"30 ASINs under a harsh throttle took {elapsed:.1f}s")


@unittest.skipUnless(os.environ.get("PV_SIM_BENCH"), "set PV_SIM_BENCH=1 to run the 300-ASIN benchmark")
class E2ESimBenchmark(E2ESimBase):
    def test_benchmark_production_tuning(self):
        catalog, rows, stats, elapsed = self.run_sim(300, ThrottlePolicy(), runner.PipelineTuning.from_config())
        self.assert_all_correct(catalog, rows)
        print(f"\n[bench] 300 ASINs in {elapsed:.1f}s ({300 / elapsed * 60:.0f} ASINs/min); "
              f"blocks fast={stats.fast.blocks} recovery={stats.recovery.blocks}; "
              f"rotations={stats.fast.rotations + stats.recovery.rotations}; sim={self.sim.stats.requests} requests")


if __name__ == "__main__":
    unittest.main()
