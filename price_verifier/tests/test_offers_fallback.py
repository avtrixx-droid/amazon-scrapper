"""
test_offers_fallback.py — the self-validating "all offers" page fallback.

  * parse_offers_page is strict: exactly one pinned price for this ASIN, or
    "unknown" (never a guess); strike-through prices are ignored
  * the pipeline switches the fallback on only after the offers page AGREED
    with the product page on real rows of the same run — a disagreement or
    an unfamiliar layout keeps it off, and those rows go to Chrome as before
  * the offers page only ever confirms a price: a 404 / no pinned offer
    there never becomes a final "not found"
  * end to end against the simulator: product pages that are always
    blocked are settled from the offers page with the right price/seller
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from price_verifier.fetcher.http_client import FetchSession
from price_verifier.fetcher.models import FetchResult
from price_verifier.fetcher.parser import parse_offers_page
from price_verifier.pipeline import runner
from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db
from price_verifier.tests.sim_amazon import SimAmazon, ThrottlePolicy, make_catalog, render_offers
from price_verifier.tests.test_pipeline_integration import (
    CAPTCHA_HTML, FakeAmazon, FakeBrowser, FakeSession, PipelineHarness, asins, fast_tuning,
)

ASIN = "B0TEST0001"


def offers_html(price=1499.0, seller="Coco Blue Retail", asin=ASIN, pinned=True, extra_price=None):
    pin = ""
    if pinned:
        extra = (f'<span class="a-price"><span class="a-offscreen">₹{extra_price:,.2f}</span></span>'
                 if extra_price else "")
        pin = (f'<div id="aod-pinned-offer"><div id="aod-offer-price">'
               f'<span class="a-price"><span class="a-offscreen">₹{price:,.2f}</span></span>{extra}'
               f'<span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">₹9,999.00</span></span>'
               f'</div><div id="aod-offer-soldBy"><span>Sold by</span><a href="/s">{seller}</a></div></div>')
    return (f'<div id="aod-container" data-asin="{asin}"><h5 id="aod-asin-title-text">Thing</h5>{pin}'
            f'<div id="aod-offer-list"><div id="aod-offer"><span class="a-price"><span class="a-offscreen">'
            f'₹199.00</span></span></div></div></div>')


class ParseOffersPageTests(unittest.TestCase):
    def test_pinned_offer(self):
        p = parse_offers_page(offers_html(), ASIN)
        self.assertEqual((p.page_kind, p.price, p.seller, p.title), ("product", 1499.0, "Coco Blue Retail", "Thing"))

    def test_strict_cases_are_unknown(self):
        cases = {
            "no pinned offer": offers_html(pinned=False),
            "two prices in the pinned offer": offers_html(extra_price=1299.0),
            "another ASIN": offers_html(asin="B0OTHER001"),
            "unfamiliar layout": offers_html().replace('id="aod-', 'id="x-'),
            "empty": "",
        }
        for name, html in cases.items():
            with self.subTest(name):
                self.assertEqual(parse_offers_page(html, ASIN).page_kind, "unknown")

    def test_block_pages(self):
        self.assertEqual(parse_offers_page(CAPTCHA_HTML, ASIN).page_kind, "captcha")

    def test_simulator_layouts(self):
        p = make_catalog(1, seed=2)
        product = next(iter(p.values()))
        self.assertEqual(parse_offers_page(render_offers(product), product.asin).price, product.price)
        self.assertEqual(parse_offers_page(render_offers(product, "changed"), product.asin).page_kind, "unknown")


class OffersAmazon(FakeAmazon):
    """FakeAmazon whose sessions also serve the offers page. `offers_price`
    lets a test make the offers page disagree; `offers_layout_ok=False`
    serves a page the parser can't read; `dp_blocked` ASINs always get a
    captcha on the product page."""

    def __init__(self, *, dp_blocked=(), offers_price=None, offers_layout_ok=True, **kw):
        super().__init__(**kw)
        self.dp_blocked = set(dp_blocked)
        self.offers_price = offers_price
        self.offers_layout_ok = offers_layout_ok
        self.offers_calls = []

    def factory(self):
        s = OffersSession(self, len(self.sessions) + 1)
        self.sessions.append(s)
        return s


class OffersSession(FakeSession):
    async def fetch(self, asin):
        if asin in self.amazon.dp_blocked:
            await asyncio.sleep(self.amazon.latency)
            return FetchResult(asin=asin, status_code=200, html=CAPTCHA_HTML)
        return await super().fetch(asin)

    async def fetch_offers(self, asin):
        self.amazon.offers_calls.append(asin)
        await asyncio.sleep(self.amazon.latency)
        price = self.amazon.offers_price or self.amazon.prices.get(asin, 1499.0)
        html = offers_html(price=price, seller="Seller", asin=asin)
        if not self.amazon.offers_layout_ok:
            html = html.replace('id="aod-', 'id="x-')
        return FetchResult(asin=asin, status_code=200, html=html, source="offers")


class OffersPipelineTests(PipelineHarness):
    async def test_validated_offers_page_settles_blocked_rows_before_chrome(self):
        all_asins = asins(20)
        amazon = OffersAmazon(dp_blocked=set(all_asins[-3:]))
        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(all_asins)
        stats = await self.go(run_id, pending, amazon, browser)
        rows = self.rows(run_id)
        self.assertTrue(stats.offers_check["enabled"], stats.offers_check)
        self.assertEqual(stats.offers_check["disagreed"], 0)
        for a in all_asins[-3:]:
            self.assertEqual(rows[a]["resolved_by"], "offers")
            self.assertEqual(rows[a]["status"], checkpoint.STATUS_MATCHED)
            self.assertEqual(rows[a]["actual_price"], 1499.0)
        self.assertEqual(browser.fetched, [], "nothing was left for Chrome")
        self.assertEqual(stats.failed, 0)
        self.assert_each_finalized_once(all_asins)

    async def test_disagreement_keeps_the_fallback_off(self):
        all_asins = asins(20)
        amazon = OffersAmazon(dp_blocked=set(all_asins[-3:]), offers_price=1399.0)
        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(all_asins)
        stats = await self.go(run_id, pending, amazon, browser)
        rows = self.rows(run_id)
        self.assertFalse(stats.offers_check["enabled"])
        self.assertGreaterEqual(stats.offers_check["disagreed"], 1)
        self.assertFalse(stats.offers.ran)
        self.assertEqual(sorted(browser.fetched), sorted(all_asins[-3:]), "blocked rows went to Chrome as before")
        self.assertFalse(any(r["resolved_by"] == "offers" for r in rows.values()))
        self.assertLessEqual(stats.offers_check["agreed"] + stats.offers_check["disagreed"]
                             + stats.offers_check["inconclusive"], fast_tuning().offers_max_samples)

    async def test_unreadable_offers_page_keeps_the_fallback_off(self):
        all_asins = asins(20)
        amazon = OffersAmazon(dp_blocked=set(all_asins[-3:]), offers_layout_ok=False)
        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(all_asins)
        stats = await self.go(run_id, pending, amazon, browser)
        self.assertFalse(stats.offers_check["enabled"])
        self.assertEqual(stats.offers_check["agreed"], 0)
        self.assertLessEqual(len(amazon.offers_calls), fast_tuning().offers_max_samples)
        self.assertEqual(sorted(browser.fetched), sorted(all_asins[-3:]))

    async def test_sampling_stops_at_the_cap(self):
        amazon = OffersAmazon(offers_layout_ok=False)
        run_id, pending = self.make_run(asins(30))
        stats = await self.go(run_id, pending, amazon, FakeBrowser(amazon))
        self.assertEqual(len(amazon.offers_calls), fast_tuning().offers_max_samples,
                         "an unreadable offers page is sampled a few times, not hammered")
        self.assertEqual(stats.offers_check["inconclusive"], fast_tuning().offers_max_samples)
        self.assertEqual(stats.failed, 0)

    async def test_sessions_without_offers_support_change_nothing(self):
        all_asins = asins(10)
        amazon = FakeAmazon()
        run_id, pending = self.make_run(all_asins)
        stats = await self.go(run_id, pending, amazon, FakeBrowser(amazon))
        self.assertEqual(stats.offers_check["agreed"] + stats.offers_check["inconclusive"], 0)
        self.assertEqual(stats.fast.attempts, 10)

    async def test_offers_page_never_finalizes_anything_but_a_price(self):
        all_asins = asins(12)
        amazon = OffersAmazon(dp_blocked={all_asins[-1]})

        browser = FakeBrowser(amazon)
        run_id, pending = self.make_run(all_asins)
        real = OffersSession.fetch_offers

        async def fetch_offers(self, asin):
            # samples see the normal offers page; the blocked row gets a 404 there
            if asin == all_asins[-1]:
                return FetchResult(asin=asin, status_code=404, html="<html>page-not-found</html>", source="offers")
            return await real(self, asin)

        with mock.patch.object(OffersSession, "fetch_offers", fetch_offers):
            stats = await self.go(run_id, pending, amazon, browser)
        row = self.rows(run_id)[all_asins[-1]]
        self.assertTrue(stats.offers.ran)
        self.assertNotEqual(row["status"], checkpoint.STATUS_NOT_FOUND)
        self.assertEqual(row["resolved_by"], "browser", "a 404 on the offers page is left for Chrome")


class OffersEndToEndTests(unittest.TestCase):
    """Real FetchSession (curl_cffi) against the simulator."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        init_db(self.db_path)
        self._dbg = mock.patch.object(runner, "_save_debug_html", lambda *a, **k: None)
        self._dbg.start()
        logging.getLogger("price_verifier").setLevel(logging.ERROR)

    def tearDown(self):
        logging.getLogger("price_verifier").setLevel(logging.NOTSET)
        self._dbg.stop()
        self.tmp.cleanup()

    def _run(self, layout):
        catalog = make_catalog(24, seed=9)
        blocked = {a for a, p in list(catalog.items()) if p.kind == "in_stock"}
        blocked = set(list(blocked)[-4:])
        sim = SimAmazon(catalog, ThrottlePolicy(burst=100, sustained_rps=50, ip_ceiling_rps=100),
                        padding_kb=20, aod_layout=layout, product_page_blocked=blocked).start()
        try:
            items = [checkpoint.RunItemRow(asin=a, expected_price=p.price, brand="") for a, p in catalog.items()]
            run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="t.csv"), items, db_path=self.db_path)
            pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
            t = fast_tuning()
            stats = asyncio.run(runner.run_pipeline(
                run_id, pending, concurrency=4, tolerance_abs=1.0, tolerance_pct=0.0, use_browser_fallback=False,
                db_path=self.db_path, tuning=t, session_factory=lambda: FetchSession(base_url=sim.base_url)))
        finally:
            sim.stop()
        rows = {r["asin"]: r for r in checkpoint.get_run_items(run_id, db_path=self.db_path)}
        return catalog, blocked, stats, rows

    def test_blocked_product_pages_settled_from_offers_page(self):
        catalog, blocked, stats, rows = self._run("standard")
        self.assertTrue(stats.offers_check["enabled"], stats.offers_check)
        for a in blocked:
            self.assertEqual(rows[a]["resolved_by"], "offers")
            self.assertEqual(rows[a]["actual_price"], catalog[a].price)
            self.assertEqual(rows[a]["seller"], catalog[a].seller)
            self.assertEqual(rows[a]["status"], checkpoint.STATUS_MATCHED)
        self.assertEqual(stats.failed, 0)

    def test_redesigned_offers_page_is_never_trusted(self):
        catalog, blocked, stats, rows = self._run("changed")
        self.assertFalse(stats.offers_check["enabled"])
        for a in blocked:
            self.assertEqual(rows[a]["status"], checkpoint.STATUS_FAILED, "left as Could Not Verify, not guessed")
        self.assertFalse(any(r["resolved_by"] == "offers" for r in rows.values()))


if __name__ == "__main__":
    unittest.main()
