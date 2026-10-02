"""
test_engine.py — the scraping engine runs in its own supervised process
(pipeline/engine.py), so a crash inside native code (the live failure:
"Windows fatal exception: access violation" in the HTML parser) can never
take the app's web server down.

  * a REAL segfault in the engine mid-run: the supervisor restarts it on
    the rows not finished yet, and the run completes
  * an engine that always dies: the run ends "crashed" (resumable) after
    the restart limit, never hangs
  * a hung engine (no heartbeat) is killed and replaced
  * Pause reaches the engine
  * the real engine_main end to end against the fake Amazon, in a child
    process, rows written to SQLite
"""

from __future__ import annotations

import faulthandler
import os
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from price_verifier.pipeline import engine
from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db


@dataclass
class Row:
    asin: str
    expected_price: float = 10.0
    brand: str = ""
    attempts: int = 0


ROWS = [Row(f"B0ENG{i:05d}") for i in range(6)]


# ── fake engines (module level: the spawn start method pickles them by name) ──
def _item(asin):
    return {"asin": asin, "status": checkpoint.STATUS_MATCHED}


def engine_ok(run_id, items, opts, out_q, cancel_ev):
    for it in items:
        out_q.put(("hb", time.time(), []))
        out_q.put(("item", _item(it.asin)))
    out_q.put(("done", {"finalized": len(items)}, False))


def engine_segfault_once(run_id, items, opts, out_q, cancel_ev):
    """Finalizes two rows, then crashes the process for real — unless a
    previous incarnation already did (marker file)."""
    marker = Path(opts["marker"])
    for n, it in enumerate(items):
        out_q.put(("item", _item(it.asin)))
        if n == 1 and not marker.exists():
            marker.write_text("crashed")
            out_q.close()
            out_q.join_thread()
            faulthandler._sigsegv()   # a real native crash, like the selectolax one
    out_q.put(("done", {"finalized": len(items)}, False))


def engine_always_dies(run_id, items, opts, out_q, cancel_ev):
    os._exit(3)


def engine_hangs(run_id, items, opts, out_q, cancel_ev):
    time.sleep(600)


def engine_waits_for_pause(run_id, items, opts, out_q, cancel_ev):
    out_q.put(("item", _item(items[0].asin)))
    while not cancel_ev.is_set():
        out_q.put(("hb", time.time(), []))
        time.sleep(0.05)
    out_q.put(("done", {"finalized": 1}, True))


def engine_raises(run_id, items, opts, out_q, cancel_ev):
    out_q.put(("error", "Traceback ...\nValueError: boom"))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.items_seen: list[str] = []
        self.phases: list[tuple] = []

    def tearDown(self):
        self.tmp.cleanup()

    def sup(self, target, **kw):
        opts = {"marker": str(Path(self.tmp.name) / "marker")}
        return engine.EngineSupervisor(
            "RUN", ROWS, opts, target=target, restart_pause=0.05,
            on_item=lambda o: self.items_seen.append(o["asin"]),
            on_phase=lambda p, d: self.phases.append((p, d)), **kw)

    def test_clean_run(self):
        res = self.sup(engine_ok).run()
        self.assertEqual((res.status, res.restarts), ("completed", 0))
        self.assertEqual(self.items_seen, [r.asin for r in ROWS])

    def test_native_crash_mid_run_is_restarted_on_the_rows_left(self):
        res = self.sup(engine_segfault_once).run()
        self.assertEqual((res.status, res.restarts), ("completed", 1))
        self.assertIn("engine process ended", res.failures[0])
        # every row exactly once: the first two from the crashed engine, the
        # other four from its replacement — nothing lost, nothing done twice
        self.assertEqual(sorted(self.items_seen), sorted(r.asin for r in ROWS))
        self.assertEqual(len(self.items_seen), len(ROWS))
        restart = [d for p, d in self.phases if p == "restart"]
        self.assertEqual(restart[0]["items"], len(ROWS) - 2)

    def test_engine_that_keeps_dying_ends_crashed_not_hung(self):
        t0 = time.monotonic()
        res = self.sup(engine_always_dies, max_restarts=2).run()
        self.assertEqual((res.status, res.restarts), ("crashed", 2))
        self.assertEqual(len(res.failures), 3)
        self.assertIn("exit code 3", res.failures[-1])
        self.assertLess(time.monotonic() - t0, 60)

    def test_hung_engine_is_killed(self):
        res = self.sup(engine_hangs, max_restarts=0, heartbeat_timeout=2.0).run()
        self.assertEqual(res.status, "crashed")
        self.assertIn("stopped responding", res.failures[0])

    def test_engine_error_is_reported(self):
        res = self.sup(engine_raises, max_restarts=0).run()
        self.assertEqual(res.status, "crashed")
        self.assertIn("ValueError: boom", res.failures[0])

    def test_pause_reaches_the_engine(self):
        sup = self.sup(engine_waits_for_pause)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("res", sup.run()))
        t.start()
        for _ in range(200):
            if self.items_seen:
                break
            time.sleep(0.05)
        sup.cancel()
        t.join(60)
        self.assertEqual(out["res"].status, "cancelled")

    def test_exit_labels(self):
        self.assertEqual(engine._exit_label(0xC0000005), "exit code 0xC0000005 (access violation)")
        self.assertEqual(engine._exit_label(-11), "killed by signal 11")
        self.assertEqual(engine._exit_label(3), "exit code 3")


class RealEngineTests(unittest.TestCase):
    """engine_main itself, in a spawned child, against the fake Amazon."""

    def test_real_engine_end_to_end(self):
        from price_verifier.tests.sim_amazon import SimAmazon, ThrottlePolicy, make_catalog

        catalog = make_catalog(8, seed=5)
        sim = SimAmazon(catalog, ThrottlePolicy(burst=100, sustained_rps=50, anon_burst=100,
                                                ip_ceiling_rps=100)).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "t.db"
            init_db(db)
            items = [checkpoint.RunItemRow(asin=p.asin, expected_price=p.price, brand=p.brand)
                     for p in catalog.values()]
            run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="t.csv"), items, db_path=db)
            seen = []
            env = {"PV_MARKETPLACE_BASE_URL": sim.base_url, "PV_DATA_DIR": tmp.name}
            with mock.patch.dict(os.environ, env):
                res = engine.EngineSupervisor(
                    run_id, items,
                    {"concurrency": 4, "tolerance_abs": 1.0, "tolerance_pct": 0.0, "use_browser": False,
                     "db_path": str(db), "log_dir": str(Path(tmp.name) / "logs")},
                    on_item=lambda o: seen.append(o), on_phase=lambda p, d: None,
                ).run()
            self.assertEqual(res.status, "completed", res.failures)
            self.assertEqual(len(seen), len(items))
            rows = checkpoint.get_run_items(run_id, db_path=db)
            self.assertFalse([r for r in rows if r["status"] == "pending"])
            self.assertEqual(res.stats["finalized"], len(items))
        finally:
            sim.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
