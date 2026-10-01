"""
test_licensing.py — license key gate for the Price Verification Tool.

  * LicenseClient: activation, product-aware requests, run authorization,
    24 h offline grace, weekly heartbeat, revoke / expiry / wrong product
  * the frozen .exe can't be pointed at another server or have the gate
    switched off through environment variables
  * the Flask app: no page without a license, activation flow, and every
    run start (new / resume / retry) authorized BEFORE the pipeline starts
"""

from __future__ import annotations

from markupsafe import escape
import io
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from price_verifier import licensing
from price_verifier.licensing import LicenseClient

KEY = "AMZ-TEST-TEST-TEST-TEST"


class FakeServer:
    """Stands in for license_server/app.py. `mode` is what the next calls
    return: "ok", a rejection reason (e.g. "revoked"), or "down"."""

    def __init__(self):
        self.mode = "ok"
        self.calls: list[tuple[str, dict]] = []

    def post(self, path, body):
        self.calls.append((path, body))
        if self.mode == "down":
            return False, {}, "network: ConnectError"
        if self.mode != "ok":
            return False, {"ok": False, "reason": self.mode}, ""
        if path == "/activate":
            return True, {"ok": True, "customer": "Lapcare", "expires_at": "2099-01-01T00:00:00Z"}, ""
        if path == "/heartbeat":
            return True, {"ok": True, "expires_at": "2099-01-01T00:00:00Z"}, ""
        return True, {"ok": True, "run_token": "t", "expires_at": "2099-01-01T00:00:00Z"}, ""


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class ClientTestBase(unittest.TestCase):
    def setUp(self):
        logging.getLogger("price_verifier.licensing").setLevel(logging.ERROR)
        self.tmp = tempfile.TemporaryDirectory()
        self.server = FakeServer()
        self.clock = Clock()
        self.client = LicenseClient(license_dir=Path(self.tmp.name), post=self.server.post, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()


class LicenseClientTests(ClientTestBase):
    def test_activation_then_authorized_run(self):
        self.assertEqual(self.client.status()["status"], "needs_activation")
        self.assertTrue(self.client.activate(" amz-test-test-test-test ").ok)
        st = self.client.status()
        self.assertEqual((st["status"], st["customer"]), ("valid", "Lapcare"))
        self.assertTrue(self.client.authorize_run(120).ok)
        path, body = self.server.calls[-1]
        self.assertEqual(path, "/authorize-run")
        self.assertEqual((body["key"], body["product"], body["asin_count"]), (KEY, "price_verifier", 120))
        self.assertEqual(body["machine_id"], licensing.get_machine_id())

    def test_every_request_names_the_product(self):
        self.client.activate(KEY)
        self.clock.advance(days=8)
        self.client.status()            # heartbeat
        self.client.authorize_run(1)
        self.assertEqual({b["product"] for _, b in self.server.calls}, {"price_verifier"})
        self.assertEqual([p for p, _ in self.server.calls], ["/activate", "/heartbeat", "/authorize-run"])

    def test_activation_rejections_are_explained(self):
        for reason in ("key_not_found", "revoked", "expired", "max_machines_reached", "product_not_licensed"):
            self.server.mode = reason
            res = self.client.activate(KEY)
            self.assertFalse(res.ok)
            self.assertEqual(res.message, licensing.MESSAGES[reason])
        self.server.mode = "down"
        self.assertEqual(self.client.activate(KEY).reason, "network")
        self.assertIsNone(self.client.load(), "nothing saved on a failed activation")

    def test_revoked_or_wrong_product_blocks_and_shows_activation(self):
        self.client.activate(KEY)
        for reason in ("revoked", "product_not_licensed", "expired"):
            self.server.mode = reason
            res = self.client.authorize_run(10)
            self.assertFalse(res.ok)
            self.assertTrue(res.relicense)
            self.assertEqual(self.client.status()["status"], reason)
        self.server.mode = "ok"
        self.assertTrue(self.client.activate(KEY).ok)
        self.assertEqual(self.client.status()["status"], "valid", "re-activating clears the block")

    def test_offline_grace_is_24_hours_from_last_authorized_run(self):
        self.client.activate(KEY)
        self.server.mode = "down"
        self.assertEqual(self.client.authorize_run(5).reason, "network", "never authorized: no grace")
        self.server.mode = "ok"
        self.assertTrue(self.client.authorize_run(5).ok)
        self.server.mode = "down"
        self.clock.advance(hours=23)
        res = self.client.authorize_run(5)
        self.assertTrue(res.ok and res.offline)
        self.clock.advance(hours=2)
        res = self.client.authorize_run(5)
        self.assertEqual((res.ok, res.reason), (False, "offline_expired"))

    def test_weekly_heartbeat(self):
        self.client.activate(KEY)
        self.clock.advance(days=6)
        self.client.status()
        self.assertEqual(len(self.server.calls), 1, "no heartbeat within a week")
        self.clock.advance(days=2)
        self.server.mode = "down"
        self.assertEqual(self.client.status()["status"], "offline", "offline is a banner, not a lock-out")
        self.server.mode = "revoked"
        self.assertEqual(self.client.status()["status"], "revoked")

    def test_local_expiry(self):
        self.client.activate(KEY)
        data = self.client.load()
        data["expires_at"] = "2026-09-01T00:00:00Z"
        self.client._save(data)
        self.assertEqual(self.client.status()["status"], "expired")

    def test_license_file_of_another_product_is_not_accepted(self):
        self.client.activate(KEY)
        data = self.client.load()
        data["product"] = "amazon_scraper"
        self.client._save(data)
        self.assertEqual(self.client.status()["status"], "needs_activation")
        self.assertFalse(self.client.authorize_run(1).ok)

    def test_machine_id_matches_the_scraper(self):
        try:
            import license as scraper_license  # repo root, needs requests + itsdangerous
        except ImportError:
            self.skipTest("scraper's license.py dependencies not installed")
        self.assertEqual(licensing.get_machine_id(), scraper_license.get_machine_id(),
                         "one PC must be ONE machine slot for a key covering both tools")


class BuildSafetyTests(unittest.TestCase):
    def test_env_overrides_work_only_from_source(self):
        with mock.patch.dict(os.environ, {"PV_LICENSE_SERVER_URL": "http://evil.example",
                                          "PV_LICENSE_DISABLED": "1"}):
            with mock.patch.object(licensing, "_is_frozen", lambda: False):
                self.assertEqual(licensing.server_url(), "http://evil.example")
                self.assertFalse(licensing.enforced())
            with mock.patch.object(licensing, "_is_frozen", lambda: True):
                self.assertEqual(licensing.server_url(), licensing.DEFAULT_SERVER_URL)
                self.assertTrue(licensing.enforced())


class AppGateTests(ClientTestBase):
    def setUp(self):
        super().setUp()
        os.environ.pop("PV_LICENSE_DISABLED", None)
        from price_verifier import app as appmod

        self.appmod = appmod
        self._patches = [
            mock.patch.object(licensing, "client", lambda: self.client),
            mock.patch.object(appmod, "_start_run", mock.Mock(return_value="RUN1")),
        ]
        for p in self._patches:
            p.start()
        appmod._LICENSE_CACHE.update(status=None, at=0.0)
        self.web = appmod.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.appmod._LICENSE_CACHE.update(status=None, at=0.0)
        super().tearDown()

    def _upload_and_confirm(self):
        csv = "ASIN,Brand,SP\n" + "".join(f"B0{100000 + i}ZZ,Lapcare,{100 + i}\n" for i in range(12))
        r = self.web.post("/upload", data={"file": (io.BytesIO(csv.encode()), "list.csv")},
                          content_type="multipart/form-data")
        map_url = r.headers["Location"]
        self.web.post(map_url, data={"col_asin": "0", "col_expected_price": "2", "col_brand": "1"})
        return map_url.rstrip("/").rsplit("/", 1)[-1]

    def test_no_page_without_a_license(self):
        for path in ("/", "/history"):
            r = self.web.get(path)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/activate"))
        self.assertEqual(self.web.get("/healthz").status_code, 200)
        self.assertEqual(self.web.get("/activate").status_code, 200)

    def test_activation_flow(self):
        self.server.mode = "key_not_found"
        r = self.web.post("/activate", data={"key": "AMZ-WRONG"})
        self.assertIn(str(escape(licensing.MESSAGES["key_not_found"])), r.get_data(as_text=True))
        self.server.mode = "ok"
        r = self.web.post("/activate", data={"key": KEY}, follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("License activated for Lapcare", page)
        self.assertIn("Licensed to Lapcare", page)
        self.assertEqual(self.web.get("/").status_code, 200)

    def test_run_is_authorized_before_it_starts(self):
        self.client.activate(KEY)
        upload_id = self._upload_and_confirm()
        r = self.web.post("/start", data={"upload_id": upload_id})
        self.assertEqual(r.status_code, 302)
        self.appmod._start_run.assert_called_once()
        path, body = self.server.calls[-1]
        self.assertEqual((path, body["asin_count"], body["product"]), ("/authorize-run", 12, "price_verifier"))

    def test_revoked_key_never_starts_a_run(self):
        self.client.activate(KEY)
        upload_id = self._upload_and_confirm()
        self.server.mode = "revoked"
        r = self.web.post("/start", data={"upload_id": upload_id})
        self.assertTrue(r.headers["Location"].endswith("/activate"))
        self.appmod._start_run.assert_not_called()
        self.assertTrue(self.web.get("/").headers["Location"].endswith("/activate"))

    def test_no_internet_keeps_the_upload_for_a_retry(self):
        self.client.activate(KEY)
        upload_id = self._upload_and_confirm()
        self.server.mode = "down"
        r = self.web.post("/start", data={"upload_id": upload_id})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Could not reach the license server", r.get_data(as_text=True))
        self.appmod._start_run.assert_not_called()
        self.server.mode = "ok"
        r = self.web.post("/start", data={"upload_id": upload_id})
        self.assertEqual(r.status_code, 302)
        self.appmod._start_run.assert_called_once()

    def test_resume_and_retry_are_authorized_too(self):
        from price_verifier.storage import checkpoint

        self.client.activate(KEY)
        items = [checkpoint.RunItemRow(asin=f"B0{100000 + i}ZZ", expected_price=10.0) for i in range(3)]
        run_id = checkpoint.create_run(checkpoint.RunConfig(input_filename="t.csv"), items)
        checkpoint.mark_run_paused(run_id)
        self.server.mode = "product_not_licensed"
        r = self.web.post(f"/resume/{run_id}")
        self.assertTrue(r.headers["Location"].endswith("/activate"))
        self.appmod._start_run.assert_not_called()
        self.server.mode = "ok"
        self.client.activate(KEY)
        self.appmod._LICENSE_CACHE.update(status=None, at=0.0)
        self.web.post(f"/retry/{run_id}")
        self.appmod._start_run.assert_called_once()
        self.assertEqual(self.server.calls[-1][1]["asin_count"], 3)

    def test_gate_can_be_switched_off_for_development_only(self):
        with mock.patch.dict(os.environ, {"PV_LICENSE_DISABLED": "1"}):
            self.appmod._LICENSE_CACHE.update(status=None, at=0.0)
            self.assertEqual(self.web.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
