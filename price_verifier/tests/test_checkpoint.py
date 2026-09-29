"""Offline SQLite checkpoint/resume tests (temp DB file per test). Run:
  python -m unittest price_verifier.tests.test_checkpoint
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_run(self, asins_and_prices, brand="TestBrand"):
        run_cfg = checkpoint.RunConfig(input_filename="t.csv")
        items = [checkpoint.RunItemRow(asin=a, expected_price=p, brand=brand) for a, p in asins_and_prices]
        return checkpoint.create_run(run_cfg, items, db_path=self.db_path)

    def test_create_run_and_pending_items(self):
        run_id = self._make_run([("B0000000AA", 100.0), ("B0000000BB", 200.0)])
        pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        self.assertEqual({p.asin for p in pending}, {"B0000000AA", "B0000000BB"})

    def test_terminal_status_excluded_from_pending(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_MATCHED, actual_price=100.0, db_path=self.db_path)
        pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        self.assertEqual(pending, [])

    def test_failed_status_stays_pending_for_resume(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_FAILED, error_reason="timeout", db_path=self.db_path)
        pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        self.assertEqual([p.asin for p in pending], ["B0000000AA"])

    def test_record_attempt_increments_without_changing_status(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        checkpoint.record_attempt(run_id, "B0000000AA", db_path=self.db_path)
        checkpoint.record_attempt(run_id, "B0000000AA", db_path=self.db_path)
        items = checkpoint.get_run_items(run_id, db_path=self.db_path)
        self.assertEqual(items[0]["attempts"], 2)
        self.assertEqual(items[0]["status"], "pending")

    def test_run_counts_recomputed_correctly(self):
        run_id = self._make_run([("B0000000AA", 100.0), ("B0000000BB", 200.0), ("B0000000CC", 300.0)])
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_MATCHED, actual_price=100.0, db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000BB", checkpoint.STATUS_MISMATCHED, actual_price=250.0, db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000CC", checkpoint.STATUS_FAILED, error_reason="timeout", db_path=self.db_path)
        run = checkpoint.get_run(run_id, db_path=self.db_path)
        self.assertEqual(run["matched"], 1)
        self.assertEqual(run["mismatched"], 1)
        self.assertEqual(run["failed"], 1)

    def test_status_flip_on_resume_does_not_double_count(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_FAILED, error_reason="timeout", db_path=self.db_path)
        run = checkpoint.get_run(run_id, db_path=self.db_path)
        self.assertEqual(run["failed"], 1)
        self.assertEqual(run["matched"], 0)
        # Resume flips it to matched
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_MATCHED, actual_price=100.0, db_path=self.db_path)
        run = checkpoint.get_run(run_id, db_path=self.db_path)
        self.assertEqual(run["failed"], 0)
        self.assertEqual(run["matched"], 1)

    def test_find_incomplete_run(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        incomplete = checkpoint.find_incomplete_run(db_path=self.db_path)
        self.assertIsNotNone(incomplete)
        self.assertEqual(incomplete["run_id"], run_id)
        checkpoint.finish_run(run_id, output_path="/tmp/x.xlsx", db_path=self.db_path)
        self.assertIsNone(checkpoint.find_incomplete_run(db_path=self.db_path))

    def test_mrp_and_seller_persisted(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        checkpoint.mark_item_result(
            run_id, "B0000000AA", checkpoint.STATUS_MISMATCHED,
            actual_price=120.0, mrp=150.0, seller="Coco Blue Retail", db_path=self.db_path,
        )
        items = checkpoint.get_run_items(run_id, db_path=self.db_path)
        self.assertEqual(items[0]["mrp"], 150.0)
        self.assertEqual(items[0]["seller"], "Coco Blue Retail")

    def test_get_brands_with_issues_excludes_clean_and_failed_only_brands(self):
        run_cfg = checkpoint.RunConfig(input_filename="t.csv")
        items = [
            checkpoint.RunItemRow(asin="B0000000AA", expected_price=100.0, brand="Lapcare"),
            checkpoint.RunItemRow(asin="B0000000BB", expected_price=200.0, brand="Lapcare"),
            checkpoint.RunItemRow(asin="B0000000CC", expected_price=300.0, brand="Portronics"),
            checkpoint.RunItemRow(asin="B0000000DD", expected_price=400.0, brand="CleanBrand"),
        ]
        run_id = checkpoint.create_run(run_cfg, items, db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_MISMATCHED, actual_price=150.0, db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000BB", checkpoint.STATUS_MATCHED, actual_price=200.0, db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000CC", checkpoint.STATUS_FAILED, error_reason="timeout", db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000DD", checkpoint.STATUS_MATCHED, actual_price=400.0, db_path=self.db_path)

        brands = checkpoint.get_brands_with_issues(run_id, db_path=self.db_path)
        # Lapcare has a mismatch -> included. Portronics only has a scrape
        # failure (not a pricing issue) -> excluded. CleanBrand is all
        # matched -> excluded.
        self.assertEqual(brands, ["Lapcare"])

    def test_run_config_pincode_defaults_when_not_supplied(self):
        run_cfg = checkpoint.RunConfig(input_filename="t.csv")
        self.assertEqual(run_cfg.pincode, "N/A")

    # ── reliability-rework additions ─────────────────────────────────────
    def test_note_item_attempt_failure_keeps_row_pending(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        checkpoint.record_attempt(run_id, "B0000000AA", db_path=self.db_path)
        checkpoint.note_item_attempt_failure(run_id, "B0000000AA", "soft_block_or_throttle", db_path=self.db_path)
        row = checkpoint.get_run_items(run_id, db_path=self.db_path)[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error_reason"], "soft_block_or_throttle")
        self.assertIsNotNone(row["last_attempt_at"])
        self.assertEqual(checkpoint.get_run(run_id, db_path=self.db_path)["failed"], 0, "not counted as failed")
        self.assertEqual([p.asin for p in checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)],
                         ["B0000000AA"])

    def test_note_item_attempt_failure_never_touches_a_final_row(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_MATCHED, actual_price=100.0,
                                    db_path=self.db_path)
        checkpoint.note_item_attempt_failure(run_id, "B0000000AA", "late error", db_path=self.db_path)
        row = checkpoint.get_run_items(run_id, db_path=self.db_path)[0]
        self.assertEqual(row["status"], checkpoint.STATUS_MATCHED)
        self.assertIsNone(row["error_reason"])

    def test_mark_item_result_persists_scraped_brand_and_resolved_by(self):
        run_id = self._make_run([("B0000000AA", 100.0)], brand="")
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_MATCHED, actual_price=100.0,
                                    scraped_brand="Lapcare", resolved_by=checkpoint.RESOLVED_BY_BROWSER,
                                    db_path=self.db_path)
        row = checkpoint.get_run_items(run_id, db_path=self.db_path)[0]
        self.assertEqual(row["scraped_brand"], "Lapcare")
        self.assertEqual(row["resolved_by"], "browser")
        self.assertEqual(row["effective_brand"], "Lapcare")

    def test_effective_brand_precedence(self):
        eb = checkpoint.effective_brand
        self.assertEqual(eb({"brand": "Lapcare", "scraped_brand": "Other"}), "Lapcare")
        self.assertEqual(eb({"brand": "  ", "scraped_brand": " Scraped "}), "Scraped")
        self.assertEqual(eb({"brand": "", "scraped_brand": None}), "Unknown Brand")
        self.assertEqual(eb({"brand": None}), "Unknown Brand")
        self.assertEqual(eb({}), "Unknown Brand")

    def test_get_brands_with_issues_groups_by_effective_brand(self):
        run_cfg = checkpoint.RunConfig(input_filename="t.csv")
        items = [
            checkpoint.RunItemRow(asin="B0000000AA", expected_price=100.0, brand=""),
            checkpoint.RunItemRow(asin="B0000000BB", expected_price=100.0, brand=""),
            checkpoint.RunItemRow(asin="B0000000CC", expected_price=100.0, brand="  "),
            checkpoint.RunItemRow(asin="B0000000DD", expected_price=100.0, brand="Zebronics"),
        ]
        run_id = checkpoint.create_run(run_cfg, items, db_path=self.db_path)
        mark = lambda asin, status, **kw: checkpoint.mark_item_result(run_id, asin, status, db_path=self.db_path, **kw)
        mark("B0000000AA", checkpoint.STATUS_MISMATCHED, actual_price=150.0, scraped_brand="boAt")
        mark("B0000000BB", checkpoint.STATUS_NO_FEATURED_OFFER)  # no scraped brand either
        mark("B0000000CC", checkpoint.STATUS_OUT_OF_STOCK, scraped_brand="boAt")
        mark("B0000000DD", checkpoint.STATUS_UNAVAILABLE, scraped_brand="Ignored")
        brands = checkpoint.get_brands_with_issues(run_id, db_path=self.db_path)
        self.assertEqual(brands, ["boAt", "Unknown Brand", "Zebronics"])
        # the Python helper agrees with the SQL grouping for every row
        rows = checkpoint.get_run_items(run_id, db_path=self.db_path)
        self.assertEqual(sorted({r["effective_brand"] for r in rows}), sorted(brands))

    def test_no_featured_offer_is_an_issue_and_counted_as_out_of_stock(self):
        self.assertIn(checkpoint.STATUS_NO_FEATURED_OFFER, checkpoint.ISSUE_STATUSES)
        self.assertIn(checkpoint.STATUS_NO_FEATURED_OFFER, checkpoint.TERMINAL_STATUSES)
        run_id = self._make_run([("B0000000AA", 100.0), ("B0000000BB", 100.0)])
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_NO_FEATURED_OFFER, db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000BB", checkpoint.STATUS_NOT_FOUND, db_path=self.db_path)
        self.assertEqual(checkpoint.get_run(run_id, db_path=self.db_path)["out_of_stock"], 2)
        self.assertEqual(checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path), [])

    def test_blank_brand_allowed_at_create(self):
        run_id = self._make_run([("B0000000AA", 100.0)], brand="")
        pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        self.assertEqual(pending[0].brand, "")

    def test_phase_and_stats_round_trip(self):
        run_id = self._make_run([("B0000000AA", 100.0)])
        self.assertIsNone(checkpoint.get_run_stats(run_id, db_path=self.db_path))
        checkpoint.set_run_phase(run_id, "recovery", db_path=self.db_path)
        checkpoint.set_run_stats(run_id, {"finalized": 1, "fast": {"blocks": 2}}, db_path=self.db_path)
        self.assertEqual(checkpoint.get_run(run_id, db_path=self.db_path)["phase"], "recovery")
        self.assertEqual(checkpoint.get_run_stats(run_id, db_path=self.db_path)["fast"]["blocks"], 2)

    def test_reopen_run_for_retry_returns_only_failed_and_pending(self):
        run_id = self._make_run([("B0000000AA", 100.0), ("B0000000BB", 200.0)])
        checkpoint.mark_item_result(run_id, "B0000000AA", checkpoint.STATUS_MATCHED, actual_price=100.0,
                                    db_path=self.db_path)
        checkpoint.mark_item_result(run_id, "B0000000BB", checkpoint.STATUS_FAILED, error_reason="x",
                                    db_path=self.db_path)
        checkpoint.finish_run(run_id, "/tmp/out.xlsx", db_path=self.db_path)
        self.assertIsNone(checkpoint.find_incomplete_run(db_path=self.db_path))
        items = checkpoint.reopen_run_for_retry(run_id, db_path=self.db_path)
        self.assertEqual([i.asin for i in items], ["B0000000BB"])
        run = checkpoint.get_run(run_id, db_path=self.db_path)
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run["finished_at"])
        self.assertEqual(checkpoint.find_incomplete_run(db_path=self.db_path)["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
