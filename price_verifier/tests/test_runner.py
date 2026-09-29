"""Offline runner tests: classify() / is_block_signal() unit tests, plus full
runs through the REAL FetchSession on an httpx.MockTransport (no network)
to prove the pipeline drives the FetchSession contract correctly. The
multi-pass / soft-block / browser scenarios live in
test_pipeline_integration.py. Run:
  python -m unittest price_verifier.tests.test_runner
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from price_verifier.fetcher.http_client import FetchSession
from price_verifier.fetcher.models import FetchResult, ParsedProduct
from price_verifier.pipeline import runner
from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db
from price_verifier.tests.test_pipeline_integration import CAPTCHA_HTML, FakeAmazon, FakeBrowser, fast_tuning, product_html

OK = FetchResult(asin="B1", status_code=200, html="<html/>")


def classify(parsed, expected=1499.0, fetch=OK, **kw):
    return runner.classify(fetch, parsed, expected, 1.0, 0, **kw)


class ClassifyTests(unittest.TestCase):
    def test_network_timeout_is_retry(self):
        fetch = FetchResult(asin="B1", status_code=None, html=None, error="timeout")
        self.assertEqual(classify(None, fetch=fetch), (checkpoint.STATUS_FAILED, "timeout", runner.ACTION_RETRY))

    def test_browser_error_is_retry(self):
        fetch = FetchResult(asin="B1", status_code=None, html=None, error="browser_error:WebDriverException",
                            source="browser")
        self.assertEqual(classify(None, fetch=fetch)[2], runner.ACTION_RETRY)

    def test_404_is_final_not_found_even_if_body_unclassifiable(self):
        fetch = FetchResult(asin="B1", status_code=404, html="<html>not found</html>")
        status, _, action = classify(ParsedProduct(page_kind="unknown"), fetch=fetch)
        self.assertEqual((status, action), (checkpoint.STATUS_NOT_FOUND, runner.ACTION_FINAL))

    def test_throttle_and_server_errors_are_retry(self):
        for code in (403, 429, 500, 503):
            fetch = FetchResult(asin="B1", status_code=code, html="<html/>")
            self.assertEqual(classify(ParsedProduct(page_kind="product", price=1.0, is_in_stock=True),
                                      fetch=fetch)[2], runner.ACTION_RETRY, code)

    def test_captcha_blocked_unknown_empty_are_retry(self):
        for kind in ("captcha", "blocked", "unknown"):
            self.assertEqual(classify(ParsedProduct(page_kind=kind))[2], runner.ACTION_RETRY, kind)
        self.assertEqual(classify(None)[1], runner.REASON_EMPTY)
        empty = FetchResult(asin="B1", status_code=200, html="")
        self.assertEqual(classify(ParsedProduct(page_kind="product"), fetch=empty)[2], runner.ACTION_RETRY)

    def test_matched_within_tolerance_and_mismatched(self):
        self.assertEqual(classify(ParsedProduct(page_kind="product", price=1500.0, is_in_stock=True)),
                         (checkpoint.STATUS_MATCHED, None, runner.ACTION_FINAL))
        self.assertEqual(classify(ParsedProduct(page_kind="product", price=1999.0, is_in_stock=True))[0],
                         checkpoint.STATUS_MISMATCHED)

    def test_price_with_unknown_availability_is_compared(self):
        status, _, action = classify(ParsedProduct(page_kind="product", price=1499.0, is_in_stock=None))
        self.assertEqual((status, action), (checkpoint.STATUS_MATCHED, runner.ACTION_FINAL))

    def test_explicit_out_of_stock_and_unavailable_are_final(self):
        self.assertEqual(classify(ParsedProduct(page_kind="product", is_in_stock=False,
                                                availability_raw="Out of Stock"))[:1],
                         (checkpoint.STATUS_OUT_OF_STOCK,))
        status, reason, action = classify(ParsedProduct(page_kind="product", is_in_stock=False,
                                                        availability_raw="Currently unavailable"))
        self.assertEqual((status, reason, action),
                         (checkpoint.STATUS_UNAVAILABLE, "Currently unavailable", runner.ACTION_FINAL))

    def test_no_featured_offer_is_final(self):
        status, _, action = classify(ParsedProduct(page_kind="product", no_featured_offer=True))
        self.assertEqual((status, action), (checkpoint.STATUS_NO_FEATURED_OFFER, runner.ACTION_FINAL))

    def test_ambiguous_pages_go_to_browser_then_finalize_there(self):
        for parsed in (ParsedProduct(page_kind="product", price=None, is_in_stock=True),
                       ParsedProduct(page_kind="product", price=None, is_in_stock=None)):
            status, _, action = classify(parsed)
            self.assertEqual((status, action), (checkpoint.STATUS_UNAVAILABLE, runner.ACTION_BROWSER))
            status, reason, action = classify(parsed, final_pass=True)
            self.assertEqual((status, action), (checkpoint.STATUS_UNAVAILABLE, runner.ACTION_FINAL))
            self.assertIn("Chrome", reason)

    def test_is_block_signal(self):
        self.assertTrue(runner.is_block_signal(FetchResult("B1", 503, "x", error="throttled_or_server_error"), None))
        self.assertTrue(runner.is_block_signal(FetchResult("B1", 429, "x"), None))
        self.assertTrue(runner.is_block_signal(OK, ParsedProduct(page_kind="captcha")))
        self.assertTrue(runner.is_block_signal(OK, ParsedProduct(page_kind="blocked")))
        self.assertFalse(runner.is_block_signal(FetchResult("B1", None, None, error="timeout"), None))
        self.assertFalse(runner.is_block_signal(FetchResult("B1", 500, "x"), None))
        self.assertFalse(runner.is_block_signal(OK, ParsedProduct(page_kind="unknown")))


class RealFetchSessionTests(unittest.IsolatedAsyncioTestCase):
    """Drives the production FetchSession (httpx backend via its
    httpx_transport test hook) through the whole pipeline."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        init_db(self.db_path)
        self._dbg = mock.patch.object(runner, "_save_debug_html", lambda *a, **k: None)
        self._dbg.start()
        logging.getLogger("price_verifier").setLevel(logging.ERROR)

    def tearDown(self):
        logging.getLogger("price_verifier").setLevel(logging.NOTSET)
        self._dbg.stop()
        self.tmpdir.cleanup()

    def make_run(self, asin_list, expected=1499.0):
        items = [checkpoint.RunItemRow(asin=a, expected_price=expected, brand="") for a in asin_list]
        run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="t.csv"), items, db_path=self.db_path)
        return run_id, checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)

    async def test_mixed_outcomes_through_real_fetch_session(self):
        calls: dict[str, int] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/":
                return httpx.Response(200, text="<html>home</html>")
            asin = path.rsplit("/", 1)[-1]
            calls[asin] = calls.get(asin, 0) + 1
            if asin == "B0ALWAYSCAP":
                return httpx.Response(200, text=CAPTCHA_HTML)
            if asin == "B0THROTTLE1" and calls[asin] == 1:
                return httpx.Response(503, text="<html>Service Unavailable</html>")
            if asin == "B0NOTFOUND1":
                return httpx.Response(404, text="<html>Looking for something?</html>")
            html = product_html(asin, 1499.0).replace(
                '<input id="add-to-cart-button"',
                '<span class="a-price a-text-price basisPrice"><span class="a-offscreen">₹1,999.00</span></span>'
                '<div id="merchant-info">Sold by <a href="/x">Coco Blue Retail</a></div>'
                '<input id="add-to-cart-button"')
            return httpx.Response(200, text=html)

        transport = httpx.MockTransport(handler)
        sessions = []

        def factory():
            s = FetchSession(base_url="https://www.amazon.in", httpx_transport=transport)
            sessions.append(s)
            return s

        run_id, pending = self.make_run(["B0MATCHOK01", "B0ALWAYSCAP", "B0THROTTLE1", "B0NOTFOUND1"])
        outcomes = []

        async def on_done(o):
            outcomes.append(o)

        stats = await runner.run_pipeline(
            run_id, pending, concurrency=3, tolerance_abs=1.0, tolerance_pct=0, db_path=self.db_path,
            on_item_done=on_done, session_factory=factory, use_browser_fallback=False, tuning=fast_tuning(),
        )

        rows = {r["asin"]: r for r in checkpoint.get_run_items(run_id, db_path=self.db_path)}
        self.assertEqual(rows["B0MATCHOK01"]["status"], checkpoint.STATUS_MATCHED)
        self.assertEqual(rows["B0MATCHOK01"]["mrp"], 1999.0)
        self.assertEqual(rows["B0MATCHOK01"]["seller"], "Coco Blue Retail")
        self.assertEqual(rows["B0MATCHOK01"]["resolved_by"], "http")
        self.assertEqual(rows["B0THROTTLE1"]["status"], checkpoint.STATUS_MATCHED, "503 retried after rotation")
        self.assertEqual(rows["B0NOTFOUND1"]["status"], checkpoint.STATUS_NOT_FOUND)
        self.assertEqual(calls["B0NOTFOUND1"], 1, "404 is never retried")
        cap = rows["B0ALWAYSCAP"]
        self.assertEqual(cap["status"], checkpoint.STATUS_FAILED)
        self.assertIn("captcha_challenge", cap["error_reason"])
        self.assertEqual(cap["attempts"], 4, "2 fast + 2 recovery attempts")
        self.assertEqual(len(outcomes), 4)
        self.assertGreaterEqual(stats.rotations, 1)
        self.assertTrue(stats.recovery.ran)
        self.assertEqual(outcomes[0].url.split("/dp/")[0], "https://www.amazon.in")

        # Failed row is retryable; terminal ones aren't re-queued.
        resumable = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        self.assertEqual([r.asin for r in resumable], ["B0ALWAYSCAP"])

    async def test_item_outcome_carries_brand_and_resolved_by(self):
        amazon = FakeAmazon()
        run_id, pending = self.make_run(["B0BRANDED01"])
        outcomes = []

        async def on_done(o):
            outcomes.append(o)

        with mock.patch.object(runner, "parse_product_page",
                               lambda html, asin: ParsedProduct(title="T", price=1499.0, brand="Lapcare",
                                                                page_kind="product", is_in_stock=True)):
            await runner.run_pipeline(run_id, pending, concurrency=1, tolerance_abs=1.0, tolerance_pct=0,
                                      db_path=self.db_path, on_item_done=on_done, session_factory=amazon.factory,
                                      browser_factory=lambda: FakeBrowser(amazon), tuning=fast_tuning())
        self.assertEqual(outcomes[0].scraped_brand, "Lapcare")
        self.assertEqual(outcomes[0].resolved_by, "http")
        row = checkpoint.get_run_items(run_id, db_path=self.db_path)[0]
        self.assertEqual(row["scraped_brand"], "Lapcare")
        self.assertEqual(row["effective_brand"], "Lapcare", "blank uploaded brand falls back to scraped brand")


if __name__ == "__main__":
    unittest.main()
