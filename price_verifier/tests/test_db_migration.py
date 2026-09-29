"""Schema-migration tests: databases created by the two schemas this tool
has already shipped must open under the current code with their data
intact, gain every new column, and then support a full pipeline run and all
reads. The old schemas are embedded verbatim (from git: 0c16484 and
3ae2060 price_verifier/storage/db.py) so this test never depends on history.
Run:
  python -m unittest price_verifier.tests.test_db_migration
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from price_verifier.excel.report import build_report
from price_verifier.pipeline import runner
from price_verifier.storage import checkpoint, db
from price_verifier.tests.test_pipeline_integration import FakeAmazon, FakeBrowser, asins, fast_tuning

# 0c16484 — first cut: no brand / mrp / seller; pincode NOT NULL with no default.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,
    input_filename  TEXT NOT NULL,
    pincode         TEXT NOT NULL,
    price_source    TEXT NOT NULL,
    tolerance_abs   REAL NOT NULL,
    tolerance_pct   REAL NOT NULL,
    concurrency     INTEGER NOT NULL,
    total_rows      INTEGER NOT NULL,
    matched         INTEGER NOT NULL DEFAULT 0,
    mismatched      INTEGER NOT NULL DEFAULT 0,
    out_of_stock    INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    output_path     TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    asin            TEXT NOT NULL,
    expected_price  REAL NOT NULL,
    actual_price    REAL,
    product_title   TEXT,
    url             TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    error_reason    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    checked_at      TEXT,
    PRIMARY KEY (run_id, asin)
);
CREATE INDEX IF NOT EXISTS idx_run_items_run_status ON run_items(run_id, status);
"""

# 3ae2060 — the build vendors have today: brand NOT NULL (no default), mrp, seller.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,
    input_filename  TEXT NOT NULL,
    pincode         TEXT NOT NULL DEFAULT 'N/A',
    price_source    TEXT NOT NULL,
    tolerance_abs   REAL NOT NULL,
    tolerance_pct   REAL NOT NULL,
    concurrency     INTEGER NOT NULL,
    total_rows      INTEGER NOT NULL,
    matched         INTEGER NOT NULL DEFAULT 0,
    mismatched      INTEGER NOT NULL DEFAULT 0,
    out_of_stock    INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    output_path     TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    asin            TEXT NOT NULL,
    brand           TEXT NOT NULL,
    expected_price  REAL NOT NULL,
    actual_price    REAL,
    mrp             REAL,
    seller          TEXT,
    product_title   TEXT,
    url             TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    error_reason    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    checked_at      TEXT,
    PRIMARY KEY (run_id, asin)
);
CREATE INDEX IF NOT EXISTS idx_run_items_run_status ON run_items(run_id, status);
CREATE INDEX IF NOT EXISTS idx_run_items_run_brand ON run_items(run_id, brand);
"""

NEW_ITEM_COLUMNS = {"brand", "mrp", "seller", "scraped_brand", "resolved_by", "last_attempt_at"}
NEW_RUN_COLUMNS = {"phase", "stats_json"}


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "old.db"
        self._dbg = mock.patch.object(runner, "_save_debug_html", lambda *a, **k: None)
        self._dbg.start()
        logging.getLogger("price_verifier").setLevel(logging.ERROR)

    def tearDown(self):
        logging.getLogger("price_verifier").setLevel(logging.NOTSET)
        self._dbg.stop()
        self.tmpdir.cleanup()

    def _build_v1(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(SCHEMA_V1)
        conn.execute("""INSERT INTO runs (run_id, started_at, status, input_filename, pincode, price_source,
                        tolerance_abs, tolerance_pct, concurrency, total_rows, matched, failed)
                        VALUES ('old1', '2026-01-01T00:00:00', 'completed', 'v1.csv', '110001', 'buybox',
                        1.0, 0.0, 15, 2, 1, 1)""")
        conn.execute("""INSERT INTO run_items (run_id, asin, expected_price, actual_price, product_title, url,
                        status, error_reason, attempts, checked_at)
                        VALUES ('old1', 'B0OLD00001', 100.0, 100.0, 'Old product', 'u', 'matched', NULL, 1, 't')""")
        conn.execute("""INSERT INTO run_items (run_id, asin, expected_price, status, error_reason, attempts)
                        VALUES ('old1', 'B0OLD00002', 200.0, 'failed', 'timeout', 3)""")
        conn.commit()
        conn.close()

    def _build_v2(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(SCHEMA_V2)
        conn.execute("""INSERT INTO runs (run_id, started_at, status, input_filename, price_source,
                        tolerance_abs, tolerance_pct, concurrency, total_rows, mismatched)
                        VALUES ('old2', '2026-02-01T00:00:00', 'running', 'v2.csv', 'buybox', 1.0, 0.0, 15, 3, 1)""")
        conn.executemany(
            """INSERT INTO run_items (run_id, asin, brand, expected_price, actual_price, mrp, seller, status, attempts)
               VALUES ('old2', ?, ?, ?, ?, ?, ?, ?, ?)""",
            [("B0OLD00010", "Lapcare", 100.0, 150.0, 199.0, "Coco Blue", "mismatched", 1),
             ("B0OLD00011", "Lapcare", 100.0, None, None, None, "pending", 0),
             ("B0OLD00012", "Portronics", 300.0, None, None, None, "failed", 3)],
        )
        conn.commit()
        conn.close()

    async def _run_new_pipeline_on_migrated_db(self):
        """A brand-new run on the migrated DB, end to end, plus every read the app/report use."""
        amazon = FakeAmazon(block_after=4, ambiguous={"B0T0000001"}, oos={"B0T0000002"})
        items = [checkpoint.RunItemRow(asin=a, expected_price=1499.0, brand="" if i % 2 else "NewBrand")
                 for i, a in enumerate(asins(8))]
        run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="new.csv"), items, db_path=self.db_path)
        pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        stats = await runner.run_pipeline(
            run_id, pending, concurrency=4, tolerance_abs=1.0, tolerance_pct=0.0, db_path=self.db_path,
            session_factory=amazon.factory, browser_factory=lambda: FakeBrowser(amazon), tuning=fast_tuning(),
        )
        self.assertEqual(stats.failed, 0)
        rows = checkpoint.get_run_items(run_id, db_path=self.db_path)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r["resolved_by"] for r in rows))
        # B0T0000002 (out of stock) was uploaded with brand "NewBrand"
        self.assertIn("NewBrand", checkpoint.get_brands_with_issues(run_id, db_path=self.db_path))
        self.assertIn("NewBrand", {r["effective_brand"] for r in rows})
        self.assertEqual(checkpoint.get_run(run_id, db_path=self.db_path)["phase"], "done")
        self.assertGreaterEqual(len(checkpoint.list_runs(db_path=self.db_path)), 2)
        return run_id

    async def test_migrates_v1_schema_without_brand_mrp_seller(self):
        self._build_v1()
        added = db.init_db(self.db_path)
        self.assertIn("run_items.brand", added)
        self.assertIn("run_items.scraped_brand", added)
        self.assertIn("runs.phase", added)
        self.assertTrue(NEW_ITEM_COLUMNS <= _columns(self.db_path, "run_items"))
        self.assertTrue(NEW_RUN_COLUMNS <= _columns(self.db_path, "runs"))
        self.assertEqual(db.init_db(self.db_path), [], "second init is a no-op")

        # old data intact and readable through the current API
        run = checkpoint.get_run("old1", db_path=self.db_path)
        self.assertEqual((run["pincode"], run["matched"], run["failed"]), ("110001", 1, 1))
        old = {r["asin"]: r for r in checkpoint.get_run_items("old1", db_path=self.db_path)}
        self.assertEqual(old["B0OLD00001"]["actual_price"], 100.0)
        self.assertEqual(old["B0OLD00001"]["product_title"], "Old product")
        self.assertEqual(old["B0OLD00001"]["brand"], "")
        self.assertEqual(old["B0OLD00001"]["effective_brand"], "Unknown Brand")
        self.assertIsNone(old["B0OLD00002"]["resolved_by"])
        # the old failed row is still retryable, and retrying it works on the migrated table
        retry = checkpoint.reopen_run_for_retry("old1", db_path=self.db_path)
        self.assertEqual([r.asin for r in retry], ["B0OLD00002"])
        checkpoint.mark_item_result("old1", "B0OLD00002", checkpoint.STATUS_MATCHED, actual_price=200.0,
                                    resolved_by="http", db_path=self.db_path)
        self.assertEqual(checkpoint.get_run("old1", db_path=self.db_path)["failed"], 0)

        await self._run_new_pipeline_on_migrated_db()

    async def test_migrates_v2_schema_currently_in_the_field(self):
        self._build_v2()
        added = db.init_db(self.db_path)
        self.assertEqual(set(added), {"run_items.scraped_brand", "run_items.resolved_by", "run_items.last_attempt_at",
                                      "runs.phase", "runs.stats_json"})
        # the vendor's interrupted run is still detected and resumable
        incomplete = checkpoint.find_incomplete_run(db_path=self.db_path)
        self.assertEqual(incomplete["run_id"], "old2")
        pending = checkpoint.get_pending_and_retryable_items("old2", db_path=self.db_path)
        self.assertEqual(sorted(p.asin for p in pending), ["B0OLD00011", "B0OLD00012"])
        self.assertEqual({p.brand for p in pending}, {"Lapcare", "Portronics"})
        old = {r["asin"]: r for r in checkpoint.get_run_items("old2", db_path=self.db_path)}
        self.assertEqual((old["B0OLD00010"]["mrp"], old["B0OLD00010"]["seller"]), (199.0, "Coco Blue"))
        self.assertEqual(checkpoint.get_brands_with_issues("old2", db_path=self.db_path), ["Lapcare"])

        # resume the old run with the new pipeline (its ASINs just get served by the fake)
        amazon = FakeAmazon()
        stats = await runner.run_pipeline(
            "old2", pending, concurrency=2, tolerance_abs=1.0, tolerance_pct=0.0, db_path=self.db_path,
            session_factory=amazon.factory, browser_factory=lambda: FakeBrowser(amazon), tuning=fast_tuning(),
        )
        self.assertEqual(stats.finalized, 2)
        old = {r["asin"]: r for r in checkpoint.get_run_items("old2", db_path=self.db_path)}
        self.assertEqual(old["B0OLD00011"]["resolved_by"], "http")
        self.assertEqual(old["B0OLD00010"]["status"], checkpoint.STATUS_MISMATCHED, "untouched")

        new_run = await self._run_new_pipeline_on_migrated_db()
        # the existing report builder still works on both old and new runs
        out = Path(self.tmpdir.name)
        self.assertTrue(build_report("old2", output_path=out / "old2.xlsx", db_path=self.db_path).exists())
        self.assertTrue(build_report(new_run, output_path=out / "new.xlsx", db_path=self.db_path).exists())

    def test_fresh_db_has_full_schema(self):
        added = db.init_db(self.db_path)
        self.assertEqual(added, [])
        self.assertTrue(NEW_ITEM_COLUMNS <= _columns(self.db_path, "run_items"))
        self.assertTrue(NEW_RUN_COLUMNS <= _columns(self.db_path, "runs"))


if __name__ == "__main__":
    unittest.main()
