"""
test_session_persistence.py — the free "look like a returning visitor"
improvements:

  * session_store: save / take-once / expiry / wrong-marketplace
  * FetchSession.export_state() only for an identity that served product
    pages; initial_state restores its cookies and fingerprint
  * the "Continue shopping" interstitial is clicked through, while a real
    CAPTCHA (image + text box) is never touched
  * end to end: a run leaves its good session behind and the next run
    starts from it (same Amazon session cookie, no new identity)
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from price_verifier import config
from price_verifier.fetcher import session_store
from price_verifier.fetcher.http_client import FetchSession
from price_verifier.pipeline import runner
from price_verifier.storage import checkpoint
from price_verifier.storage.db import init_db
from price_verifier.tests.sim_amazon import SimAmazon, ThrottlePolicy, make_catalog

BASE = "https://www.amazon.in"
PRODUCT = ('<html><body><span id="productTitle">Thing</span>'
           '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">₹499.00</span></span></div>'
           '<input type="hidden" id="ASIN" value="B0TEST0001"></body></html>')

CONTINUE_PAGE = """<!doctype html><html><body>
<div class="a-box"><h4>Click the button below to continue shopping</h4>
<form method="get" action="/errors/validateCaptcha" name="">
  <input type=hidden name="amzn" value="abc123token" />
  <input type=hidden name="amzn-r" value="/dp/B0TEST0001" />
  <input type=hidden name="field-keywords" value="XKWQPT" />
  <span class="a-button a-button-primary"><span class="a-button-inner">
    <button type="submit" class="a-button-text" alt="Continue shopping">Continue shopping</button>
  </span></span>
</form></div></body></html>"""

REAL_CAPTCHA = """<!doctype html><html><body>
<form method="get" action="/errors/validateCaptcha" name="">
  <input type=hidden name="amzn" value="abc123token" />
  <input type=hidden name="amzn-r" value="/dp/B0TEST0001" />
  <img src="https://images-na.ssl-images-amazon.com/captcha/xyz/Captcha_abc.jpg">
  <input autocomplete="off" type="text" id="captchacharacters" name="field-keywords">
  <button type="submit" class="a-button-text">Continue shopping</button>
</form></body></html>"""


def run(coro):
    return asyncio.run(coro)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.json"
        self.state = {"backend": "curl_cffi", "target": "chrome146", "base_url": BASE,
                      "cookies": [{"name": "session-id", "value": "1", "domain": ".amazon.in", "path": "/"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_take_once(self):
        self.assertTrue(session_store.save(self.state, self.path))
        got = session_store.take(self.path, base_url=BASE)
        self.assertEqual(got["cookies"], self.state["cookies"])
        self.assertIsNone(session_store.take(self.path, base_url=BASE), "a saved identity is offered only once")

    def test_expired_or_other_marketplace_is_ignored(self):
        session_store.save(self.state, self.path)
        self.assertIsNone(session_store.take(self.path, base_url="http://127.0.0.1:9"))
        session_store.save(self.state, self.path)
        data = json.loads(self.path.read_text())
        data["saved_at"] = time.time() - 13 * 3600
        self.path.write_text(json.dumps(data))
        self.assertIsNone(session_store.take(self.path, base_url=BASE))

    def test_garbage_file_is_harmless(self):
        self.path.write_text("{not json")
        self.assertIsNone(session_store.take(self.path, base_url=BASE))
        self.assertFalse(self.path.exists())


class ExportRestoreTests(unittest.TestCase):
    def test_export_only_after_a_product_page(self):
        def handler(req):
            if req.url.path == "/":
                return httpx.Response(200, text="home", headers={"set-cookie": "session-id=S1; Path=/"})
            return httpx.Response(200, text=PRODUCT)

        async def go():
            s = FetchSession(base_url=BASE, httpx_transport=httpx.MockTransport(handler))
            await s.start()
            before = s.export_state()
            await s.fetch("B0TEST0001")
            after = s.export_state()
            await s.close()
            return before, after

        before, after = run(go())
        self.assertIsNone(before, "a fresh identity is not worth saving")
        self.assertEqual(after["backend"], "httpx")
        self.assertIn("session-id", {c["name"] for c in after["cookies"]})

    def test_blocked_or_captcha_identity_is_not_exported(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(503, text="Sorry! Something went wrong!"))

        async def go():
            s = FetchSession(base_url=BASE, httpx_transport=transport)
            await s.start()
            await s.fetch("B0TEST0001")
            state = s.export_state()
            await s.close()
            return state

        self.assertIsNone(run(go()))

    def test_initial_state_restores_cookies_and_fingerprint(self):
        seen = []

        def handler(req):
            seen.append(req.headers.get("cookie", ""))
            return httpx.Response(200, text=PRODUCT)

        state = {"backend": "httpx", "target": "145", "base_url": BASE,
                 "cookies": [{"name": "session-id", "value": "OLD-GOOD", "domain": ".amazon.in", "path": "/"}]}

        async def go():
            s = FetchSession(base_url=BASE, httpx_transport=httpx.MockTransport(handler), initial_state=state)
            await s.start()
            await s.fetch("B0TEST0001")
            target, restored = s.impersonate_target, s.restored
            await s.close()
            return target, restored

        target, restored = run(go())
        self.assertTrue(restored)
        self.assertEqual(target, "145")
        self.assertTrue(all("session-id=OLD-GOOD" in c for c in seen), seen)

    def test_state_for_another_backend_is_ignored(self):
        state = {"backend": "curl_cffi", "target": "chrome146", "base_url": BASE,
                 "cookies": [{"name": "session-id", "value": "X", "domain": ".amazon.in", "path": "/"}]}

        async def go():
            s = FetchSession(base_url=BASE, httpx_transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text=PRODUCT)), initial_state=state)
            await s.start()
            restored = s.restored
            await s.close()
            return restored

        self.assertFalse(run(go()))


class ContinueShoppingTests(unittest.TestCase):
    def _session(self, first_page):
        calls = []

        def handler(req):
            calls.append((req.url.path, dict(req.url.params), req.headers.get("referer")))
            if req.url.path == "/":
                return httpx.Response(200, text="home")
            if req.url.path == "/errors/validateCaptcha":
                return httpx.Response(200, text=PRODUCT)
            return httpx.Response(200, text=first_page)

        return FetchSession(base_url=BASE, httpx_transport=httpx.MockTransport(handler)), calls

    def test_interstitial_is_clicked_through(self):
        s, calls = self._session(CONTINUE_PAGE)

        async def go():
            await s.start()
            r = await s.fetch("B0TEST0001")
            state = s.export_state()
            await s.close()
            return r, state

        r, state = run(go())
        self.assertIsNone(r.error)
        self.assertIn("productTitle", r.html)
        self.assertEqual(s.interstitials_passed, 1)
        path, params, referer = calls[-1]
        self.assertEqual(path, "/errors/validateCaptcha")
        self.assertEqual(params, {"amzn": "abc123token", "amzn-r": "/dp/B0TEST0001", "field-keywords": "XKWQPT"})
        self.assertEqual(referer, f"{BASE}/dp/B0TEST0001")
        self.assertIsNotNone(state, "the identity that got through is worth keeping")

    def test_landing_on_the_homepage_after_the_interstitial_asks_again(self):
        """Seen live: past "continue shopping", Amazon serves its homepage
        (ue_pty "Gateway"), not the product. The product is requested again."""
        calls = []
        home = '<html><script>var ue_pty = "Gateway";</script><body>home</body></html>'

        def handler(req):
            calls.append(req.url.path)
            if req.url.path == "/errors/validateCaptcha":
                return httpx.Response(200, text=home)
            if req.url.path == "/dp/B0TEST0001" and calls.count("/dp/B0TEST0001") == 1:
                return httpx.Response(200, text=CONTINUE_PAGE)
            return httpx.Response(200, text=PRODUCT if req.url.path.startswith("/dp/") else home)

        s = FetchSession(base_url=BASE, httpx_transport=httpx.MockTransport(handler))

        async def go():
            await s.start()
            r = await s.fetch("B0TEST0001")
            await s.close()
            return r

        r = run(go())
        self.assertIn("productTitle", r.html)
        self.assertEqual(calls[-3:], ["/dp/B0TEST0001", "/errors/validateCaptcha", "/dp/B0TEST0001"])

    def test_real_captcha_is_never_submitted(self):
        s, calls = self._session(REAL_CAPTCHA)

        async def go():
            await s.start()
            r = await s.fetch("B0TEST0001")
            await s.close()
            return r

        r = run(go())
        self.assertEqual(s.interstitials_passed, 0)
        self.assertNotIn("/errors/validateCaptcha", [c[0] for c in calls])
        self.assertIn("captchacharacters", r.html)

    def test_form_to_another_site_is_not_followed(self):
        page = CONTINUE_PAGE.replace('action="/errors/validateCaptcha"',
                                     'action="https://evil.example/errors/validateCaptcha"')
        s, calls = self._session(page)

        async def go():
            await s.start()
            await s.fetch("B0TEST0001")
            await s.close()

        run(go())
        self.assertEqual(s.interstitials_passed, 0)


class RunToRunPersistenceTests(unittest.TestCase):
    """Production code path (no session_factory): run 1 saves its good
    identity, run 2's first request carries the same Amazon session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        init_db(self.db_path)
        self.catalog = make_catalog(12, seed=5)
        self.sim = SimAmazon(self.catalog, ThrottlePolicy(burst=100, sustained_rps=50, ip_ceiling_rps=100),
                             padding_kb=20).start()
        self._patches = [
            mock.patch.object(config, "MARKETPLACE_BASE_URL", self.sim.base_url),
            mock.patch.object(config, "DATA_DIR", Path(self.tmp.name)),
            mock.patch.object(runner, "_save_debug_html", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.sim.stop()
        self.tmp.cleanup()

    def _run(self):
        items = [checkpoint.RunItemRow(asin=a, expected_price=p.price, brand="") for a, p in self.catalog.items()]
        run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="t.csv"), items, db_path=self.db_path)
        pending = checkpoint.get_pending_and_retryable_items(run_id, db_path=self.db_path)
        t = runner.PipelineTuning.from_config()
        t.fast_initial_rps = t.fast_max_rps = 50.0
        return asyncio.run(runner.run_pipeline(run_id, pending, concurrency=4, tolerance_abs=1.0, tolerance_pct=0.0,
                                               use_browser_fallback=False, db_path=self.db_path, tuning=t))

    def test_second_run_starts_as_a_returning_visitor(self):
        state_file = Path(self.tmp.name) / "session_state.json"
        stats1 = self._run()
        self.assertEqual(stats1.failed, 0)
        self.assertTrue(state_file.exists(), "a good session is left for the next run")
        saved = json.loads(state_file.read_text())
        saved_sid = next(c["value"] for c in saved["cookies"] if c["name"] == "session-id")
        identities_after_run1 = self.sim.stats.identities

        stats2 = self._run()
        self.assertEqual(stats2.failed, 0)
        self.assertEqual(self.sim.stats.identities, identities_after_run1,
                         "run 2 reused the saved Amazon session instead of starting a new one")
        self.assertIn(f"s:{saved_sid}", self.sim.stats.by_identity)


if __name__ == "__main__":
    unittest.main()
