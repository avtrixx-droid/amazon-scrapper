"""
Offline parser tests — no network, no browser. Run:
  python -m unittest price_verifier.tests.test_parser

These validate parser.py's logic against synthetic/fixture HTML that mirrors
real Amazon markup structure (class/id names are public, documented in this
repo's own CLAUDE.md selector table). They do NOT prove the selectors match
a live amazon.in response today — see README.md "Known gaps".
"""

from __future__ import annotations

import unittest
from pathlib import Path

from price_verifier.fetcher.parser import classify_page, parse_money, parse_product_page

FIXTURES = Path(__file__).parent / "fixtures"


class ParseMoneyTests(unittest.TestCase):
    def test_rupee_symbol_and_commas(self):
        self.assertEqual(parse_money("₹1,499.00"), 1499.0)

    def test_plain_number(self):
        self.assertEqual(parse_money("999.50"), 999.5)

    def test_not_found_returns_none(self):
        self.assertIsNone(parse_money("Not Found"))

    def test_none_input(self):
        self.assertIsNone(parse_money(None))

    def test_zero_treated_as_missing(self):
        self.assertIsNone(parse_money("₹0.00"))


class ProductPageTests(unittest.TestCase):
    def setUp(self):
        self.html = (FIXTURES / "sample_product_page.html").read_text()

    def test_classifies_as_product(self):
        self.assertEqual(classify_page(self.html, "B09W9FND7M"), "product")

    def test_extracts_title_price_availability(self):
        p = parse_product_page(self.html, "B09W9FND7M")
        self.assertEqual(p.page_kind, "product")
        self.assertEqual(p.title, "Lapcare Webcam 720p with Built-in Microphone")
        self.assertEqual(p.price, 1499.0)
        self.assertTrue(p.is_in_stock)

    def test_asin_not_present_is_unknown(self):
        self.assertEqual(classify_page(self.html, "B0000000ZZ"), "unknown")


class FalsePositiveScopeTests(unittest.TestCase):
    """The exact bug class this repo's delivery scraper already hit once
    (CLAUDE.md: "today my package arrived" matched review text as a
    delivery date). Price extraction should be equally immune: a sponsored
    or unrelated price elsewhere on the page must not be picked up."""

    def test_price_outside_buybox_is_ignored(self):
        html = """
        <html><body>
        <input type="hidden" id="ASIN" value="B0TARGETXX">
        <div id="sponsoredProducts">
          <span class="a-price"><span class="a-offscreen">₹99.00</span></span>
        </div>
        <div id="rightCol">
          <span id="productTitle">Target Product</span>
          <div id="corePriceDisplay_desktop_feature_div">
            <span class="a-price apexPriceToPay"><span class="a-offscreen">₹2,499.00</span></span>
          </div>
          <div id="availability"><span>In stock.</span></div>
          <input id="add-to-cart-button" name="submit.add-to-cart" type="submit">
        </div>
        </body></html>
        """
        p = parse_product_page(html, "B0TARGETXX")
        self.assertEqual(p.price, 2499.0, "must read the buy-box price, not the sponsored ₹99 one")


class AvailabilityTests(unittest.TestCase):
    def test_out_of_stock_phrase(self):
        html = """
        <html><body>
        <input type="hidden" id="ASIN" value="B0OOSTEST1">
        <span id="productTitle">Some Item</span>
        <div id="availability"><span>Currently unavailable.</span></div>
        </body></html>
        """
        p = parse_product_page(html, "B0OOSTEST1")
        self.assertFalse(p.is_in_stock)
        self.assertIn("unavailable", (p.availability_raw or "").lower())

    def test_no_cta_no_explicit_phrase_is_ambiguous(self):
        html = """
        <html><body>
        <input type="hidden" id="ASIN" value="B0AMBIGUOU">
        <span id="productTitle">Some Item</span>
        <div id="availability"><span>Ships in 2-3 weeks.</span></div>
        </body></html>
        """
        p = parse_product_page(html, "B0AMBIGUOU")
        self.assertIsNone(p.is_in_stock)


class PageClassificationTests(unittest.TestCase):
    def test_captcha_page(self):
        html = "<html><body><form action='/errors/validateCaptcha'><input id='captchacharacters'></form></body></html>"
        self.assertEqual(classify_page(html, "B09W9FND7M"), "captcha")

    def test_blocked_page(self):
        html = "<html><body>Sorry, we just need to make sure you're not a robot.</body></html>"
        self.assertEqual(classify_page(html, "B09W9FND7M"), "blocked")

    def test_not_found_page(self):
        html = "<html><body><h1>Looking for something?</h1><p>We're sorry. The Web address you entered is not a functioning page.</p></body></html>"
        self.assertEqual(classify_page(html, "B09W9FND7M"), "not_found")


if __name__ == "__main__":
    unittest.main()
