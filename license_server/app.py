"""
app.py — License activation + heartbeat server for AmazonScraper.

Flask + SQLite. Designed to run on Render (free tier with a persistent disk).
Secrets are read from environment variables — never hard-code them here.

Endpoints:
  POST /activate        — first-time key activation, binds to machine_id
  POST /heartbeat       — periodic re-validation (catches revocation)
  GET  /healthz         — Render health check
  POST /admin/issue     — issue a new key (Bearer auth)
  GET  /admin/list      — list all keys (Bearer auth)
  POST /admin/extend    — extend a key by N days (Bearer auth)
  POST /admin/revoke    — revoke a key (Bearer auth)
  POST /admin/release-machine — release an activation slot (Bearer auth)
  GET  /admin/info      — key + activations detail (Bearer auth)
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("license_server")

# ── Config from env ────────────────────────────────────────────────────────────
SIGNING_SECRET = os.environ.get("LICENSE_SIGNING_SECRET")
ADMIN_TOKEN = os.environ.get("LICENSE_ADMIN_TOKEN")
DB_PATH = os.environ.get("LICENSE_DB_PATH", "licenses.db")

if not SIGNING_SECRET:
    log.error("LICENSE_SIGNING_SECRET env var is required.")
    sys.exit(1)
if not ADMIN_TOKEN:
    log.error("LICENSE_ADMIN_TOKEN env var is required.")
    sys.exit(1)

# itsdangerous serializer (TimedSerializer so tokens carry an issued-at timestamp)
serializer = URLSafeTimedSerializer(SIGNING_SECRET, salt="amzscraper-license-v1")

# Key alphabet — Crockford-ish, no ambiguous characters (no 0,1,I,L,O)
KEY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

app = Flask(__name__)


# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_db():
    """Per-request SQLite connection — stashed on Flask's `g`."""
    db = getattr(g, "_db", None)
    if db is None:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g._db = db
    return db


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables on first run. Idempotent."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS keys (
                key TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                max_machines INTEGER NOT NULL DEFAULT 1,
                revoked INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS activations (
                key TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                app_version TEXT DEFAULT '',
                PRIMARY KEY (key, machine_id),
                FOREIGN KEY (key) REFERENCES keys(key)
            );
            """
        )
        conn.commit()
        log.info("DB initialised at %s", DB_PATH)
    finally:
        conn.close()


# ── Utilities ──────────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    """Parse the ISO strings we write (always UTC, trailing Z)."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        # Fallback for legacy / fromisoformat values
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)


def generate_key() -> str:
    """AMZ-XXXX-XXXX-XXXX-XXXX where X is from KEY_ALPHABET."""
    groups = []
    for _ in range(4):
        groups.append("".join(secrets.choice(KEY_ALPHABET) for _ in range(4)))
    return "AMZ-" + "-".join(groups)


def sign_token(payload: dict) -> str:
    return serializer.dumps(payload)


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"ok": False, "reason": "auth_required"}), 401
        token = header.split(" ", 1)[1].strip()
        # constant-time compare
        if not secrets.compare_digest(token, ADMIN_TOKEN):
            return jsonify({"ok": False, "reason": "auth_invalid"}), 401
        return fn(*args, **kwargs)

    return wrapper


def json_body() -> dict:
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {}
    return data


# ── Public endpoints ───────────────────────────────────────────────────────────
@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "time": now_iso()})


@app.route("/activate", methods=["POST"])
def activate():
    data = json_body()
    key = (data.get("key") or "").strip().upper()
    machine_id = (data.get("machine_id") or "").strip()
    app_version = (data.get("app_version") or "").strip()

    if not key or not machine_id:
        return jsonify({"ok": False, "reason": "bad_request"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM keys WHERE key = ?", (key,)).fetchone()
    if row is None:
        log.info("activate: key_not_found key=%s", key)
        return jsonify({"ok": False, "reason": "key_not_found"}), 404

    if row["revoked"]:
        log.info("activate: revoked key=%s", key)
        return jsonify({"ok": False, "reason": "revoked"}), 403

    expires_at = row["expires_at"]
    if parse_iso(expires_at) < datetime.now(timezone.utc):
        log.info("activate: expired key=%s exp=%s", key, expires_at)
        return jsonify({"ok": False, "reason": "expired", "expires_at": expires_at}), 403

    existing = db.execute(
        "SELECT * FROM activations WHERE key = ? AND machine_id = ?",
        (key, machine_id),
    ).fetchone()
    now = now_iso()

    if existing:
        # Already activated on this machine — refresh the heartbeat row.
        db.execute(
            "UPDATE activations SET last_heartbeat = ?, app_version = ? "
            "WHERE key = ? AND machine_id = ?",
            (now, app_version, key, machine_id),
        )
        db.commit()
        log.info("activate: refresh key=%s machine=%s", key, machine_id[:12])
    else:
        # New machine — check slot count.
        count = db.execute(
            "SELECT COUNT(*) AS c FROM activations WHERE key = ?", (key,)
        ).fetchone()["c"]
        max_machines = row["max_machines"]
        if count >= max_machines:
            log.info(
                "activate: max_machines_reached key=%s count=%d max=%d",
                key, count, max_machines,
            )
            return jsonify({
                "ok": False,
                "reason": "max_machines_reached",
                "max_machines": max_machines,
            }), 403

        db.execute(
            "INSERT INTO activations (key, machine_id, activated_at, last_heartbeat, app_version) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, machine_id, now, now, app_version),
        )
        db.commit()
        log.info("activate: new machine key=%s machine=%s ver=%s",
                 key, machine_id[:12], app_version)

    token = sign_token({
        "key": key,
        "machine_id": machine_id,
        "expires_at": expires_at,
        "issued_at": now,
    })
    return jsonify({
        "ok": True,
        "expires_at": expires_at,
        "customer": row["customer"],
        "signed_token": token,
        "server_time": now,
    })


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data = json_body()
    key = (data.get("key") or "").strip().upper()
    machine_id = (data.get("machine_id") or "").strip()
    app_version = (data.get("app_version") or "").strip()

    if not key or not machine_id:
        return jsonify({"ok": False, "reason": "bad_request"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM keys WHERE key = ?", (key,)).fetchone()
    if row is None or row["revoked"]:
        # Treat unknown key the same as revoked — client should re-activate
        # (or be blocked if revocation was intentional).
        log.info("heartbeat: revoked-or-missing key=%s", key)
        return jsonify({"ok": False, "reason": "revoked"}), 403

    activation = db.execute(
        "SELECT * FROM activations WHERE key = ? AND machine_id = ?",
        (key, machine_id),
    ).fetchone()
    if activation is None:
        # Admin released the machine — treat like revoked.
        log.info("heartbeat: no activation row key=%s machine=%s",
                 key, machine_id[:12])
        return jsonify({"ok": False, "reason": "revoked"}), 403

    expires_at = row["expires_at"]
    if parse_iso(expires_at) < datetime.now(timezone.utc):
        log.info("heartbeat: expired key=%s exp=%s", key, expires_at)
        return jsonify({"ok": False, "reason": "expired", "expires_at": expires_at}), 403

    now = now_iso()
    db.execute(
        "UPDATE activations SET last_heartbeat = ?, app_version = ? "
        "WHERE key = ? AND machine_id = ?",
        (now, app_version, key, machine_id),
    )
    db.commit()
    log.info("heartbeat: ok key=%s machine=%s ver=%s",
             key, machine_id[:12], app_version)

    token = sign_token({
        "key": key,
        "machine_id": machine_id,
        "expires_at": expires_at,
        "issued_at": now,
    })
    return jsonify({
        "ok": True,
        "expires_at": expires_at,
        "signed_token": token,
        "server_time": now,
    })


# ── Admin endpoints ────────────────────────────────────────────────────────────
@app.route("/admin/issue", methods=["POST"])
@require_admin
def admin_issue():
    data = json_body()
    customer = (data.get("customer") or "").strip()
    days = int(data.get("days") or 0)
    max_machines = int(data.get("max_machines") or 1)
    notes = (data.get("notes") or "").strip()

    if not customer or days <= 0 or max_machines <= 0:
        return jsonify({"ok": False, "reason": "bad_request"}), 400

    db = get_db()
    # Retry until we hit an unused key — collision probability is essentially zero.
    for _ in range(8):
        key = generate_key()
        row = db.execute("SELECT 1 FROM keys WHERE key = ?", (key,)).fetchone()
        if not row:
            break
    else:
        return jsonify({"ok": False, "reason": "key_gen_failed"}), 500

    issued_at = now_iso()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    db.execute(
        "INSERT INTO keys (key, customer, issued_at, expires_at, max_machines, revoked, notes) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        (key, customer, issued_at, expires_at, max_machines, notes),
    )
    db.commit()
    log.info("admin: issued key=%s customer=%s days=%d max=%d",
             key, customer, days, max_machines)

    return jsonify({
        "ok": True,
        "key": key,
        "customer": customer,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "max_machines": max_machines,
        "notes": notes,
    })


@app.route("/admin/list", methods=["GET"])
@require_admin
def admin_list():
    db = get_db()
    rows = db.execute(
        "SELECT k.key, k.customer, k.issued_at, k.expires_at, k.max_machines, "
        "k.revoked, k.notes, "
        "(SELECT COUNT(*) FROM activations a WHERE a.key = k.key) AS machines_used "
        "FROM keys k ORDER BY k.issued_at DESC"
    ).fetchall()
    return jsonify({
        "ok": True,
        "keys": [dict(r) for r in rows],
    })


@app.route("/admin/extend", methods=["POST"])
@require_admin
def admin_extend():
    data = json_body()
    key = (data.get("key") or "").strip().upper()
    days = int(data.get("days") or 0)
    if not key or days == 0:
        return jsonify({"ok": False, "reason": "bad_request"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM keys WHERE key = ?", (key,)).fetchone()
    if not row:
        return jsonify({"ok": False, "reason": "key_not_found"}), 404

    old_expiry = parse_iso(row["expires_at"])
    new_expiry = old_expiry + timedelta(days=days)
    new_expiry_str = new_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE keys SET expires_at = ? WHERE key = ?", (new_expiry_str, key))
    db.commit()
    log.info("admin: extended key=%s by %d days → %s", key, days, new_expiry_str)
    return jsonify({"ok": True, "key": key, "expires_at": new_expiry_str})


@app.route("/admin/revoke", methods=["POST"])
@require_admin
def admin_revoke():
    data = json_body()
    key = (data.get("key") or "").strip().upper()
    if not key:
        return jsonify({"ok": False, "reason": "bad_request"}), 400

    db = get_db()
    row = db.execute("SELECT 1 FROM keys WHERE key = ?", (key,)).fetchone()
    if not row:
        return jsonify({"ok": False, "reason": "key_not_found"}), 404

    db.execute("UPDATE keys SET revoked = 1 WHERE key = ?", (key,))
    db.commit()
    log.info("admin: revoked key=%s", key)
    return jsonify({"ok": True, "key": key, "revoked": True})


@app.route("/admin/release-machine", methods=["POST"])
@require_admin
def admin_release_machine():
    data = json_body()
    key = (data.get("key") or "").strip().upper()
    machine_id = (data.get("machine_id") or "").strip()
    if not key or not machine_id:
        return jsonify({"ok": False, "reason": "bad_request"}), 400

    db = get_db()
    cur = db.execute(
        "DELETE FROM activations WHERE key = ? AND machine_id = ?",
        (key, machine_id),
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"ok": False, "reason": "activation_not_found"}), 404
    log.info("admin: released key=%s machine=%s", key, machine_id[:12])
    return jsonify({"ok": True, "key": key, "machine_id": machine_id})


@app.route("/admin/info", methods=["GET"])
@require_admin
def admin_info():
    key = (request.args.get("key") or "").strip().upper()
    if not key:
        return jsonify({"ok": False, "reason": "bad_request"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM keys WHERE key = ?", (key,)).fetchone()
    if not row:
        return jsonify({"ok": False, "reason": "key_not_found"}), 404

    activations = db.execute(
        "SELECT machine_id, activated_at, last_heartbeat, app_version "
        "FROM activations WHERE key = ? ORDER BY activated_at",
        (key,),
    ).fetchall()
    return jsonify({
        "ok": True,
        "key": dict(row),
        "activations": [dict(a) for a in activations],
    })


# ── Init + entry ───────────────────────────────────────────────────────────────
init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    log.info("Starting license server on 0.0.0.0:%d (db=%s)", port, DB_PATH)
    app.run(host="0.0.0.0", port=port)
