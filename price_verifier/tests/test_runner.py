"""Offline pipeline runner tests: classify() unit tests plus a full async
run through a mocked HTTP transport (httpx.MockTransport — no real network).
Run:
  python -m unittest price_verifier.tests.test_runner
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from price_verifier.fetcher.models import FetchResult, ParsedProduct
from price_verifier.fetcher.session_bootstrap import BootstrappedSession
from price_verifier.pipeline import runner
from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db


class ClassifyTests(unittest.TestCase):
    def test_network_timeout_is_retryable(self):
        fetch = FetchResult(asin="B1", status_code=None, html=None, error="timeout")
        status, reason, retryable = runner.classify(fetch, None, 100.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_FAILED)
        self.assertTrue(retryable)

    def test_404_is_not_found_never_retried(self):
        fetch = FetchResult(asin="B1", status_code=404, html="<html>not found</html>")
        parsed = ParsedProduct(page_kind="unknown")  # even if the parser can't classify the body
        status, reason, retryable = runner.classify(fetch, parsed, 100.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_NOT_FOUND)
        self.assertFalse(retryable)

    def test_captcha_is_retryable(self):
        fetch = FetchResult(asin="B1", status_code=200, html="<html/>")
        parsed = ParsedProduct(page_kind="captcha")
        status, reason, retryable = runner.classify(fetch, parsed, 100.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_FAILED)
        self.assertTrue(retryable)

    def test_matched_price(self):
        fetch = FetchResult(asin="B1", status_code=200, html="<html/>")
        parsed = ParsedProduct(page_kind="product", price=1499.0, is_in_stock=True)
        status, reason, retryable = runner.classify(fetch, parsed, 1499.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_MATCHED)
        self.assertFalse(retryable)

    def test_mismatched_price(self):
        fetch = FetchResult(asin="B1", status_code=200, html="<html/>")
        parsed = ParsedProduct(page_kind="product", price=1999.0, is_in_stock=True)
        status, reason, retryable = runner.classify(fetch, parsed, 1499.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_MISMATCHED)

    def test_in_stock_missing_price_fails_without_retry(self):
        # Static HTML won't change on a retry if the selector genuinely
        # doesn't match this layout — retrying wastes a request and delays
        # the Failed-sheet diagnostic the spec asks for.
        fetch = FetchResult(asin="B1", status_code=200, html="<html/>")
        parsed = ParsedProduct(page_kind="product", price=None, is_in_stock=True)
        status, reason, retryable = runner.classify(fetch, parsed, 1499.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_FAILED)
        self.assertFalse(retryable)

    def test_out_of_stock_phrase_maps_to_out_of_stock_status(self):
        fetch = FetchResult(asin="B1", status_code=200, html="<html/>")
        parsed = ParsedProduct(page_kind="product", is_in_stock=False, availability_raw="Out of Stock")
        status, reason, retryable = runner.classify(fetch, parsed, 100.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_OUT_OF_STOCK)

    def test_other_unavailable_phrase_maps_to_unavailable_status(self):
        fetch = FetchResult(asin="B1", status_code=200, html="<html/>")
        parsed = ParsedProduct(page_kind="product", is_in_stock=False, availability_raw="Currently unavailable")
        status, reason, retryable = runner.classify(fetch, parsed, 100.0, 1.0, 0)
        self.assertEqual(status, checkpoint.STATUS_UNAVAILABLE)


PRODUCT_HTML = """
<html><body>
<input type="hidden" id="ASIN" value="{asin}">
<div id="rightCol">
  <span id="productTitle">Product {asin}</span>
  <div id="corePriceDisplay_desktop_feature_div">
    <span class="a-price apexPriceToPay"><span class="a-offscreen">{price}</span></span>
  </div>
  <div id="availability"><span>In stock.</span></div>
  <input id="add-to-cart-button" name="submit.add-to-cart" type="submit">
</div>
</body></html>
"""


class RunPipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Full pipeline run against a mocked HTTP transport — validates the
    fetch -> parse -> classify -> checkpoint -> resume wiring together, not
    just each piece in isolation."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        init_db(self.db_path)
        self.session = BootstrappedSession(cookies={}, user_agent="test", pincode="110001", city="Delhi", captured_at=0)

    def tearDown(self):
        self.tmpdir.cleanup()

    async def test_mixed_outcomes_and_resume(self):
        call_count = {}

        def handler(request: httpx.Request) -> httpx.Response:
            asin = request.url.path.rsplit("/", 1)[-1]
            call_count[asin] = call_count.get(asin, 0) + 1
            if asin == "B0ALWAYSFAIL":
                return httpx.Response(200, text="<html><body>captchacharacters</body></html>")
            return httpx.Response(200, text=PRODUCT_HTML.format(asin=asin, price="1499.00"))

        transport = httpx.MockTransport(handler)
        original_build_client = runner.build_client
        runner.build_client = lambda cookies, user_agent: httpx.AsyncClient(transport=transport, base_url="https://www.amazon.in")

        try:
            run_cfg = checkpoint.RunConfig(input_filename="t.csv", pincode="110001", concurrency=3)
            items = [
                checkpoint.RunItemRow(asin="B0MATCHOK01", expected_price=1499.0),
                checkpoint.RunItemRow(asin="B0ALWAYSFAIL", expected_price=100.0),
            ]
            run_id = checkpoint.create_run(run_cfg, items, db_path=self.db_path)
            pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)

            await runner.run_pipeline(run_id, pending, self.session, concurrency=3, tolerance_abs=1.0, tolerance_pct=0, db_path=self.db_path)

            run = checkpoint.get_run(run_id, db_path=self.db_path)
            self.assertEqual(run["matched"], 1)
            self.assertEqual(run["failed"], 1)
            self.assertEqual(call_count["B0ALWAYSFAIL"], 3, "should retry up to MAX_ATTEMPTS then stop")

            # Resume: only the failed row should be re-queued
            resumable = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
            self.assertEqual([r.asin for r in resumable], ["B0ALWAYSFAIL"])
        finally:
            runner.build_client = original_build_client


if __name__ == "__main__":
    unittest.main()
