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

    def _make_run(self, asins_and_prices):
        run_cfg = checkpoint.RunConfig(input_filename="t.csv", pincode="110001")
        items = [checkpoint.RunItemRow(asin=a, expected_price=p) for a, p in asins_and_prices]
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


if __name__ == "__main__":
    unittest.main()
