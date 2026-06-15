"""
tests/test_server.py — Integration tests for the license server's HTTP API.

Drives the REAL Flask routes in license_server/app.py through Flask's test
client. psycopg2 is replaced with a lightweight SQLite-backed shim so the
actual SQL and endpoint control-flow run unchanged — this is what guarantees
"nobody can misuse this": the security branches (revoked / expired / no
activation / max machines) are exercised against the real handler code.

Lifecycle covered:
  issue → activate → authorize-run (ok) → revoke → authorize-run (blocked)
  plus expiry, max-machines, release-machine, and admin auth.

Run:  python -m unittest tests.test_server -v
"""

from __future__ import annotations

import importlib
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest


# ── psycopg2 shim (SQLite-backed) ───────────────────────────────────────────
# Built BEFORE importing app.py. Translates the Postgres-isms the server uses
# (%s placeholders, SERIAL PRIMARY KEY) onto a shared on-disk SQLite database.

_SQLITE_PATH = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


def _translate(sql: str) -> str:
    sql = sql.replace("%s", "?")
    sql = sql.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    return sql


class _FakeCursor:
    def __init__(self, conn, as_dict):
        self._conn = conn
        self._as_dict = as_dict
        self._cur = conn.cursor()

    def execute(self, sql, params=()):
        self._cur.execute(_translate(sql), params)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return dict(row) if self._as_dict else row

    def fetchall(self):
        rows = self._cur.fetchall()
        return [dict(r) for r in rows] if self._as_dict else rows

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self._conn = sqlite3.connect(_SQLITE_PATH)
        self._conn.row_factory = sqlite3.Row

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._conn, as_dict=cursor_factory is not None)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _install_psycopg2_shim():
    fake = types.ModuleType("psycopg2")
    fake.connect = lambda *a, **k: _FakeConn()
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object  # only used as a truthy marker by the shim
    fake.extras = extras
    sys.modules["psycopg2"] = fake
    sys.modules["psycopg2.extras"] = extras


_install_psycopg2_shim()

# Required env vars (app.py exits if missing).
os.environ.setdefault("LICENSE_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("LICENSE_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DATABASE_URL", "postgres://fake/test")

# Make license_server importable as a package path.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "license_server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as srv  # noqa: E402  (import after shim + env setup)

ADMIN = {"Authorization": "Bearer test-admin-token"}
MACHINE = "machine-aaaa-1111"


def _fresh_db():
    """Drop & recreate all tables for a clean test."""
    conn = sqlite3.connect(_SQLITE_PATH)
    for t in ("runs", "activations", "keys"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    srv.init_db()


class ServerLifecycleTests(unittest.TestCase):

    def setUp(self):
        _fresh_db()
        self.c = srv.app.test_client()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _issue(self, days=30, machines=1, customer="TestCo"):
        r = self.c.post("/admin/issue",
                        json={"customer": customer, "days": days,
                              "max_machines": machines},
                        headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["key"]

    def _activate(self, key, machine=MACHINE):
        return self.c.post("/activate",
                           json={"key": key, "machine_id": machine,
                                 "app_version": "2.0"})

    def _authorize(self, key, machine=MACHINE, asins=10, pins=3):
        return self.c.post("/authorize-run",
                           json={"key": key, "machine_id": machine,
                                 "asin_count": asins, "pincode_count": pins,
                                 "app_version": "2.0"})

    # ── the core "no misuse" lifecycle ──────────────────────────────────────
    def test_full_happy_path(self):
        key = self._issue()
        self.assertTrue(self._activate(key).get_json()["ok"])
        r = self._authorize(key)
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["run_token"])
        self.assertIn("expires_at", body)

    def test_revoke_blocks_runs(self):
        key = self._issue()
        self._activate(key)
        self.assertTrue(self._authorize(key).get_json()["ok"])  # works first

        rv = self.c.post("/admin/revoke", json={"key": key}, headers=ADMIN)
        self.assertTrue(rv.get_json()["ok"])

        r = self._authorize(key)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["reason"], "revoked")

    def test_run_without_activation_blocked(self):
        key = self._issue()
        # No /activate call — machine not bound.
        r = self._authorize(key)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["reason"], "revoked")

    def test_unknown_key_blocked(self):
        r = self._authorize("AMZ-NOPE-NOPE-NOPE-NOPE")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["reason"], "revoked")

    def test_expired_key_blocked(self):
        key = self._issue(days=30)
        self._activate(key)               # bind machine while still valid
        # Backdate expiry directly in the DB to simulate the clock passing.
        conn = sqlite3.connect(_SQLITE_PATH)
        conn.execute("UPDATE keys SET expires_at = ? WHERE key = ?",
                     ("2000-01-01T00:00:00Z", key))
        conn.commit()
        conn.close()
        r = self._authorize(key)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["reason"], "expired")

    def test_release_machine_blocks_runs(self):
        key = self._issue()
        self._activate(key)
        self.assertTrue(self._authorize(key).get_json()["ok"])

        rel = self.c.post("/admin/release-machine",
                          json={"key": key, "machine_id": MACHINE},
                          headers=ADMIN)
        self.assertTrue(rel.get_json()["ok"])

        r = self._authorize(key)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["reason"], "revoked")

    def test_max_machines_enforced(self):
        key = self._issue(machines=1)
        self.assertTrue(self._activate(key, machine="machine-1").get_json()["ok"])
        # Second distinct machine must be refused.
        r2 = self._activate(key, machine="machine-2")
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.get_json()["reason"], "max_machines_reached")

    def test_reactivation_same_machine_ok(self):
        key = self._issue(machines=1)
        self.assertTrue(self._activate(key, machine="machine-1").get_json()["ok"])
        # Same machine re-activating must still succeed (idempotent).
        self.assertTrue(self._activate(key, machine="machine-1").get_json()["ok"])

    def test_run_is_logged(self):
        key = self._issue()
        self._activate(key)
        self._authorize(key, asins=42, pins=5)
        runs = self.c.get("/admin/runs", headers=ADMIN).get_json()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["asin_count"], 42)
        self.assertEqual(runs[0]["pincode_count"], 5)

    # ── admin auth ──────────────────────────────────────────────────────────
    def test_admin_requires_token(self):
        r = self.c.get("/admin/list")
        self.assertEqual(r.status_code, 401)

    def test_admin_rejects_wrong_token(self):
        r = self.c.get("/admin/list",
                       headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_authorize_run_bad_request(self):
        r = self.c.post("/authorize-run", json={"key": ""})
        self.assertEqual(r.status_code, 400)

    def test_healthz(self):
        r = self.c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
