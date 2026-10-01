"""
test_app.py — license server tests against a REAL Postgres.

Skipped unless LICENSE_TEST_DATABASE_URL points at a throwaway database
(every test drops and recreates the tables there):

    LICENSE_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres \\
        python -m unittest license_server.test_app

Covers the per-product licensing added for the Price Verification Tool, and
above all that it is backward compatible: a database created by the
previous server version (no products/product columns) migrates in place,
its keys keep unlocking the Amazon Scraper, and requests from scraper builds
that never send `product` keep working.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest

DB_URL = os.environ.get("LICENSE_TEST_DATABASE_URL")
ADMIN = "test-admin-token"

OLD_SCHEMA = """
DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS activations; DROP TABLE IF EXISTS keys;
CREATE TABLE keys (key TEXT PRIMARY KEY, customer TEXT NOT NULL, issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, max_machines INTEGER NOT NULL DEFAULT 1,
    revoked INTEGER NOT NULL DEFAULT 0, notes TEXT DEFAULT '');
CREATE TABLE activations (key TEXT NOT NULL, machine_id TEXT NOT NULL, activated_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL, app_version TEXT DEFAULT '', PRIMARY KEY (key, machine_id),
    FOREIGN KEY (key) REFERENCES keys(key));
CREATE TABLE runs (id SERIAL PRIMARY KEY, key TEXT NOT NULL, machine_id TEXT NOT NULL,
    asin_count INTEGER NOT NULL DEFAULT 0, pincode_count INTEGER NOT NULL DEFAULT 0,
    app_version TEXT DEFAULT '', run_token TEXT NOT NULL, requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, FOREIGN KEY (key) REFERENCES keys(key));
INSERT INTO keys VALUES ('AMZ-OLD1-OLD1-OLD1-OLD1', 'Existing Customer', '2026-01-01T00:00:00Z',
    '2099-01-01T00:00:00Z', 2, 0, '');
INSERT INTO runs (key, machine_id, run_token, requested_at, expires_at)
    VALUES ('AMZ-OLD1-OLD1-OLD1-OLD1', 'm-old', 't', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z');
"""


def _load_app():
    os.environ["DATABASE_URL"] = DB_URL
    os.environ["LICENSE_SIGNING_SECRET"] = "test-signing-secret"
    os.environ["LICENSE_ADMIN_TOKEN"] = ADMIN
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    if "app" in sys.modules:
        return importlib.reload(sys.modules["app"])
    return importlib.import_module("app")


@unittest.skipUnless(DB_URL, "set LICENSE_TEST_DATABASE_URL to a throwaway Postgres to run")
class LicenseServerTests(unittest.TestCase):
    OLD_KEY = "AMZ-OLD1-OLD1-OLD1-OLD1"

    def setUp(self):
        import psycopg2

        conn = psycopg2.connect(DB_URL)
        with conn, conn.cursor() as cur:
            cur.execute(OLD_SCHEMA)
        conn.close()
        self.mod = _load_app()       # import runs init_db() -> migrates the old schema
        self.mod.init_db()           # and running it again must be harmless
        self.client = self.mod.app.test_client()

    # ── helpers ────────────────────────────────────────────────────────────
    def post(self, path, body, admin=False):
        headers = {"Authorization": f"Bearer {ADMIN}"} if admin else {}
        r = self.client.post(path, json=body, headers=headers)
        return r.status_code, r.get_json()

    def issue(self, products=None, machines=1):
        body = {"customer": "Test Co", "days": 30, "max_machines": machines}
        if products is not None:
            body["products"] = products
        code, data = self.post("/admin/issue", body, admin=True)
        self.assertEqual(code, 200, data)
        return data

    # ── backward compatibility ─────────────────────────────────────────────
    def test_existing_key_and_old_clients_keep_working(self):
        # old scraper build: no "product" field at all
        code, data = self.post("/activate", {"key": self.OLD_KEY, "machine_id": "m1"})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["products"], ["amazon_scraper"])
        code, data = self.post("/authorize-run", {"key": self.OLD_KEY, "machine_id": "m1", "asin_count": 5})
        self.assertEqual(code, 200, data)
        code, data = self.post("/heartbeat", {"key": self.OLD_KEY, "machine_id": "m1"})
        self.assertEqual(code, 200, data)

    def test_existing_key_does_not_unlock_the_new_product(self):
        code, data = self.post("/activate", {"key": self.OLD_KEY, "machine_id": "m1", "product": "price_verifier"})
        self.assertEqual((code, data["reason"]), (403, "product_not_licensed"))

    def test_old_runs_rows_migrated(self):
        import psycopg2

        conn = psycopg2.connect(DB_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT product FROM runs WHERE machine_id = 'm-old'")
            self.assertEqual(cur.fetchone()[0], "amazon_scraper")
            cur.execute("SELECT products FROM keys WHERE key = %s", (self.OLD_KEY,))
            self.assertEqual(cur.fetchone()[0], "amazon_scraper")
        conn.close()

    # ── per-product keys ───────────────────────────────────────────────────
    def test_price_verifier_only_key(self):
        key = self.issue("price_verifier")["key"]
        body = {"key": key, "machine_id": "m1", "product": "price_verifier"}
        self.assertEqual(self.post("/activate", body)[0], 200)
        code, data = self.post("/authorize-run", dict(body, asin_count=120))
        self.assertEqual(code, 200, data)
        self.assertTrue(data["run_token"])
        # ...and it does NOT unlock the scraper (old scraper builds send no product)
        code, data = self.post("/authorize-run", {"key": key, "machine_id": "m1"})
        self.assertEqual((code, data["reason"]), (403, "product_not_licensed"))

    def test_runs_are_logged_per_product(self):
        key = self.issue("amazon_scraper,price_verifier")["key"]
        self.post("/authorize-run", {"key": key, "machine_id": "m1", "product": "price_verifier", "asin_count": 7})
        self.post("/authorize-run", {"key": key, "machine_id": "m1", "asin_count": 3})
        r = self.client.get(f"/admin/runs?key={key}", headers={"Authorization": f"Bearer {ADMIN}"})
        runs = r.get_json()["runs"]
        self.assertEqual(sorted((x["product"], x["asin_count"]) for x in runs),
                         [("amazon_scraper", 3), ("price_verifier", 7)])

    def test_one_machine_slot_shared_across_products(self):
        key = self.issue("amazon_scraper,price_verifier", machines=1)["key"]
        self.assertEqual(self.post("/activate", {"key": key, "machine_id": "m1"})[0], 200)
        self.assertEqual(self.post("/activate", {"key": key, "machine_id": "m1", "product": "price_verifier"})[0], 200)
        code, data = self.post("/activate", {"key": key, "machine_id": "m2", "product": "price_verifier"})
        self.assertEqual((code, data["reason"]), (403, "max_machines_reached"))
        info = self.client.get(f"/admin/info?key={key}", headers={"Authorization": f"Bearer {ADMIN}"}).get_json()
        self.assertEqual(len(info["activations"]), 1)
        self.assertEqual(info["key"]["products"], "amazon_scraper,price_verifier")

    def test_set_products_grants_and_removes(self):
        code, data = self.post("/admin/set-products",
                               {"key": self.OLD_KEY, "products": "amazon_scraper, price_verifier"}, admin=True)
        self.assertEqual((code, data["products"]), (200, ["amazon_scraper", "price_verifier"]))
        self.assertEqual(self.post("/activate", {"key": self.OLD_KEY, "machine_id": "m1",
                                                 "product": "price_verifier"})[0], 200)
        self.post("/admin/set-products", {"key": self.OLD_KEY, "products": ["amazon_scraper"]}, admin=True)
        code, data = self.post("/heartbeat", {"key": self.OLD_KEY, "machine_id": "m1", "product": "price_verifier"})
        self.assertEqual((code, data["reason"]), (403, "product_not_licensed"))

    def test_revoke_still_kills_every_product(self):
        key = self.issue("amazon_scraper,price_verifier")["key"]
        self.post("/admin/revoke", {"key": key}, admin=True)
        for product in ("amazon_scraper", "price_verifier"):
            code, data = self.post("/authorize-run", {"key": key, "machine_id": "m1", "product": product})
            self.assertEqual((code, data["reason"]), (403, "revoked"))

    def test_bad_input_rejected(self):
        self.assertEqual(self.post("/activate", {"key": self.OLD_KEY, "machine_id": "m1",
                                                 "product": "Bad Product!"})[0], 400)
        self.assertEqual(self.post("/admin/issue", {"customer": "x", "days": 3, "products": "bad name"},
                                   admin=True)[0], 400)
        self.assertEqual(self.post("/admin/set-products", {"key": self.OLD_KEY, "products": ""}, admin=True)[0], 400)
        self.assertEqual(self.post("/admin/set-products", {"key": "AMZ-NOPE", "products": "price_verifier"},
                                   admin=True)[0], 404)
        self.assertEqual(self.post("/admin/set-products", {"key": self.OLD_KEY, "products": "price_verifier"})[0], 401)

    def test_issue_defaults_to_scraper(self):
        data = self.issue()
        self.assertEqual(data["products"], ["amazon_scraper"])

    def test_price_verifier_client_end_to_end(self):
        """The real price_verifier.licensing client against this real server."""
        import tempfile
        from pathlib import Path

        from price_verifier.licensing import LicenseClient

        def bridge(path, body):
            r = self.client.post(path, json=body)
            data = r.get_json() or {}
            return (r.status_code == 200 and bool(data.get("ok"))), data, ""

        with tempfile.TemporaryDirectory() as d:
            pv = LicenseClient(license_dir=Path(d), post=bridge)
            scraper_only = self.issue()["key"]
            res = pv.activate(scraper_only)
            self.assertEqual((res.ok, res.reason), (False, "product_not_licensed"))

            key = self.issue("price_verifier")["key"]
            self.assertTrue(pv.activate(key).ok)
            self.assertEqual(pv.status()["status"], "valid")
            self.assertTrue(pv.authorize_run(250).ok)

            self.post("/admin/revoke", {"key": key}, admin=True)
            res = pv.authorize_run(10)
            self.assertEqual((res.ok, res.reason), (False, "revoked"))
            self.assertEqual(pv.status()["status"], "revoked")

            self.post("/admin/unrevoke", {"key": key}, admin=True)
            self.assertTrue(pv.activate(key).ok)
            self.assertTrue(pv.authorize_run(10).ok)


if __name__ == "__main__":
    unittest.main()
