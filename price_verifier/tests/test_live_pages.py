"""
test_live_pages.py — the parser against REAL amazon.in pages, fetched by
the tool's own fetcher on a Windows runner (tests/live_probe.py) and saved
under tests/fixtures/live/ (big product pages gzipped).

Why these exist: the hand-written fixtures are a few KB; real product pages
are 0.7–1.5 MB. On real pages selectolax's Modest engine crashed the whole
process ("Windows fatal exception: access violation" in parser._index) —
every vendor run died within seconds of starting, "connection refused".
Each parse below runs in its own subprocess, so a native crash shows up as
a failed test instead of killing the test run.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import unittest
from pathlib import Path

from price_verifier.fetcher import parser
from price_verifier.fetcher.models import FetchResult
from price_verifier.pipeline import runner

LIVE = Path(__file__).parent / "fixtures" / "live"

_CHILD = r"""
import gzip, json, sys
from price_verifier.fetcher.parser import parse_product_page
path, asin = sys.argv[1], sys.argv[2]
raw = open(path, "rb").read()
html = (gzip.decompress(raw) if path.endswith(".gz") else raw).decode("utf-8")
p = parse_product_page(html, asin)
print(json.dumps(dict(kind=p.page_kind, price=p.price, mrp=p.mrp, seller=p.seller, in_stock=p.is_in_stock,
                      availability=p.availability_raw, title=p.title, brand=p.brand,
                      no_featured_offer=p.no_featured_offer)))
"""


def parse_isolated(name: str, asin: str) -> dict:
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(LIVE / name), asin],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"parsing {name} crashed the process (exit {proc.returncode}): "
                             f"{proc.stderr.strip()[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def read(name: str) -> str:
    raw = (LIVE / name).read_bytes()
    return (gzip.decompress(raw) if name.endswith(".gz") else raw).decode("utf-8")


class RealProductPageTests(unittest.TestCase):
    def test_parser_uses_the_lexbor_engine(self):
        self.assertEqual(parser.HTMLParser.__module__, "selectolax.lexbor")

    def test_real_unavailable_product_pages(self):
        for name, asin, title in (
            ("unavailable_B0CHX1W1XY.html.gz", "B0CHX1W1XY", "Apple iPhone 15 (128 GB) - Black"),
            ("unavailable_B0D1XD1ZV3.html.gz", "B0D1XD1ZV3", "Apple AirPods Pro 2"),
        ):
            with self.subTest(name=name):
                p = parse_isolated(name, asin)   # must not crash — the vendor's bug
                self.assertEqual(p["kind"], "product")
                self.assertTrue(p["title"].startswith(title), p["title"])
                self.assertIs(p["in_stock"], False)
                self.assertEqual(p["availability"], "Currently unavailable.")
                self.assertIsNone(p["price"])
                self.assertEqual(p["brand"], "Apple")

    def test_real_unavailable_page_is_a_final_answer(self):
        html = read("unavailable_B0CHX1W1XY.html.gz")
        fetch = FetchResult(asin="B0CHX1W1XY", status_code=200, html=html)
        status, _reason, action = runner.classify(fetch, parser.parse_product_page(html, "B0CHX1W1XY"),
                                                  50000.0, 1.0, 0.0)
        self.assertEqual((status, action), ("unavailable", runner.ACTION_FINAL))


class RealBlockPageTests(unittest.TestCase):
    def test_akamai_interstitial_is_a_block(self):
        """HTTP 200, ~2 KB JavaScript proof-of-work page. It used to come out
        as an "unknown page": retried without slowing down or rotating."""
        html = read("akamai_interstitial_B0BSHF7WHW.html")
        self.assertEqual(parser.classify_page(html, "B0BSHF7WHW"), "blocked")
        parsed = parser.parse_product_page(html, "B0BSHF7WHW")
        fetch = FetchResult(asin="B0BSHF7WHW", status_code=200, html=html)
        self.assertTrue(runner.is_block_signal(fetch, parsed))
        _status, _reason, action = runner.classify(fetch, parsed, 100.0, 1.0, 0.0)
        self.assertEqual(action, runner.ACTION_RETRY)

    def test_real_404_is_not_found(self):
        html = read("not_found_404_B09G9FPHY6.html")
        fetch = FetchResult(asin="B09G9FPHY6", status_code=404, html=html)
        status, _reason, action = runner.classify(fetch, parser.parse_product_page(html, "B09G9FPHY6"),
                                                  100.0, 1.0, 0.0)
        self.assertEqual((status, action), ("not_found", runner.ACTION_FINAL))


if __name__ == "__main__":
    unittest.main()
