"""
license.py — Client-side license module for AmazonScraper.

Pure Python, no Flask. Importable from gui.py.

Responsibilities:
  • Compute a stable machine_id per platform.
  • Persist license state in the user's app-data dir as license.json.
  • Authorize each scraping run via the server (POST /authorize-run).
  • Heartbeat to the server every 7 days; tolerate up to 24 hours offline.

The signing secret is NO LONGER required on the client. Token verification
is optional (backwards compat for old builds). The server is the source of
truth — every run must be authorized via /authorize-run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from itsdangerous import BadSignature, URLSafeTimedSerializer

# ── Configuration ──────────────────────────────────────────────────────────────
# IMPORTANT: replace this URL with your deployed Render service URL.
LICENSE_SERVER_URL = "https://amazon-scraper-license.onrender.com"
APP_VERSION = "2.0"
HEARTBEAT_INTERVAL_DAYS = 7
OFFLINE_GRACE_DAYS = 1  # reduced from 14 — server authorization is now required
OFFLINE_GRACE_HOURS = 24
LICENSE_FILENAME = "license.json"

# Network timeouts — Render free tier can take 30s to wake from sleep, so be
# generous on the first call. Heartbeats happen weekly so this is fine.
REQUEST_TIMEOUT_SECONDS = 35

log = logging.getLogger("license")


# ── Embedded signing secret + server URL ──────────────────────────────────────
# In built distributions, CI writes _build_config.py with constants BEFORE
# PyInstaller runs, so the values get baked into the bundle. In dev we fall
# back to environment variables so you can run gui.py without writing a file.
def _get_signing_secret() -> str | None:
    """Return signing secret if available, else None.

    In new builds, no secret is baked — the server is the sole authority.
    Old builds may still have a baked secret for backwards compatibility.
    """
    try:
        from _build_config import SECRET  # type: ignore[import-not-found]
        if SECRET:
            return SECRET
    except ImportError:
        pass
    secret = os.environ.get("AMZ_LICENSE_SECRET")
    if secret:
        return secret
    log.debug("No signing secret configured — using server-only auth")
    return None


def _get_server_url() -> str:
    """Return license server URL. Prefers baked _build_config.SERVER_URL."""
    try:
        from _build_config import SERVER_URL  # type: ignore[import-not-found]
        if SERVER_URL:
            return SERVER_URL
    except ImportError:
        pass
    return LICENSE_SERVER_URL


def _serializer() -> URLSafeTimedSerializer | None:
    secret = _get_signing_secret()
    if secret is None:
        return None
    return URLSafeTimedSerializer(secret, salt="amzscraper-license-v1")


# ── Machine ID ─────────────────────────────────────────────────────────────────
def _read_windows_machine_guid() -> str:
    try:
        import winreg  # type: ignore[import-not-found]
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as k:
            value, _ = winreg.QueryValueEx(k, "MachineGuid")
            return str(value)
    except Exception:
        return ""


def _read_mac_platform_uuid() -> str:
    try:
        out = subprocess.check_output(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                # line looks like:  "IOPlatformUUID" = "1A2B3C4D-..."
                _, _, rhs = line.partition("=")
                return rhs.strip().strip('"')
    except Exception:
        pass
    return ""


def _read_linux_machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return ""


def get_machine_id() -> str:
    """Stable per-machine identifier. Returns first 32 hex chars of SHA-256."""
    plat = sys.platform
    platform_value = ""
    if plat.startswith("win"):
        platform_value = _read_windows_machine_guid()
    elif plat == "darwin":
        platform_value = _read_mac_platform_uuid()
    else:
        platform_value = _read_linux_machine_id()

    # Always blend in uuid.getnode() (MAC) so we still have *something* if the
    # platform reader fails (corporate locked-down Windows, no /etc/machine-id).
    mac_part = format(uuid.getnode(), "x")
    combined = f"{platform_value}|{mac_part}|{plat}".encode("utf-8")
    return hashlib.sha256(combined).hexdigest()[:32]


# ── License storage ────────────────────────────────────────────────────────────
def _app_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "AmazonScraper"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AmazonScraper"
    return Path.home() / ".config" / "AmazonScraper"


def get_license_path() -> Path:
    d = _app_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / LICENSE_FILENAME


def load_license() -> dict | None:
    path = get_license_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def save_license(data: dict) -> None:
    """Atomic write — tmp file + rename."""
    path = get_license_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Failed to save license.json: %s", exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# ── Token verification ─────────────────────────────────────────────────────────
def verify_signed_token(token: str) -> dict | None:
    """Verify the itsdangerous-signed token locally. Returns payload or None.

    If no signing secret is configured (new builds), returns None — the caller
    must fall through to timestamp-based checks instead.
    """
    if not token:
        return None
    ser = _serializer()
    if ser is None:
        return None
    try:
        payload = ser.loads(token, max_age=None)
        if isinstance(payload, dict):
            return payload
    except BadSignature:
        return None
    except Exception as exc:
        log.warning("Token verify error: %s", exc)
    return None


# ── Networking ─────────────────────────────────────────────────────────────────
def _post(path: str, body: dict) -> tuple[bool, dict, str]:
    """Return (ok, response_json, network_error_reason).

    `ok` is True iff we got HTTP 200 *and* the response body is `ok: true`.
    `network_error_reason` is "" on any 2xx/4xx (server reachable),
    non-empty when the server is unreachable (DNS fail, timeout, etc.)."""
    url = _get_server_url().rstrip("/") + path
    try:
        r = requests.post(
            url,
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return False, {}, f"network: {exc}"

    try:
        data = r.json()
    except ValueError:
        return False, {}, f"bad_json (HTTP {r.status_code})"

    if r.ok and data.get("ok"):
        return True, data, ""
    return False, data, ""


# ── User-facing error messages ─────────────────────────────────────────────────
ERROR_MESSAGES = {
    "key_not_found":
        "We couldn't find that license key. Double-check the characters and try again.",
    "revoked":
        "This license has been revoked. Please contact support to resolve it.",
    "expired":
        "This license has expired. Please contact support to renew.",
    "max_machines_reached":
        "This license has reached its maximum number of activated machines. "
        "Contact support to release a slot.",
    "bad_request":
        "The activation request was malformed. Please try again.",
    "network":
        "Could not reach the license server. Check your internet connection and try again.",
}


def _friendly(reason: str, data: dict | None = None) -> str:
    if reason in ERROR_MESSAGES:
        return ERROR_MESSAGES[reason]
    if reason.startswith("network"):
        return ERROR_MESSAGES["network"]
    return "Activation failed. Please try again or contact support."


# ── Public API ─────────────────────────────────────────────────────────────────
def activate(key: str) -> tuple[bool, str]:
    """Activate a key. On success persists license.json and returns (True, '')."""
    key = (key or "").strip().upper()
    if not key:
        return False, "Please enter a license key."

    machine_id = get_machine_id()
    body = {"key": key, "machine_id": machine_id, "app_version": APP_VERSION}
    ok, data, net_err = _post("/activate", body)
    if not ok:
        if net_err:
            log.warning("activate network error: %s", net_err)
            return False, _friendly("network")
        reason = (data or {}).get("reason", "unknown")
        log.info("activate failed: %s", reason)
        return False, _friendly(reason, data)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    license_data = {
        "key": key,
        "machine_id": machine_id,
        "customer": data.get("customer", ""),
        "expires_at": data.get("expires_at", ""),
        "signed_token": data.get("signed_token", ""),
        "activated_at": now,
        "last_check": now,
        "app_version": APP_VERSION,
    }
    save_license(license_data)
    return True, ""


def heartbeat() -> tuple[bool, str]:
    """Heartbeat to the server. Updates license.json. Returns (ok, reason)."""
    data = load_license()
    if not data:
        return False, "no_license"
    key = data.get("key", "")
    machine_id = data.get("machine_id") or get_machine_id()

    body = {"key": key, "machine_id": machine_id, "app_version": APP_VERSION}
    ok, resp, net_err = _post("/heartbeat", body)
    if not ok:
        if net_err:
            return False, "network"
        return False, (resp or {}).get("reason", "unknown")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data.update({
        "signed_token": resp.get("signed_token", data.get("signed_token", "")),
        "expires_at": resp.get("expires_at", data.get("expires_at", "")),
        "last_check": now,
        "machine_id": machine_id,
    })
    save_license(data)
    return True, ""


# ── Date helpers ───────────────────────────────────────────────────────────────
def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 86400.0


# ── Gate ───────────────────────────────────────────────────────────────────────
def check_license_status() -> dict:
    """UI status check called by gui.py on page load. Returns a dict with
    "status" key. This is for display purposes — the hard gate is
    authorize_run() which must succeed before any scraping starts."""
    data = load_license()
    if not data:
        return {"status": "needs_activation", "reason": "no_license"}

    token = data.get("signed_token", "")
    payload = verify_signed_token(token)

    # If no signing secret is available (new builds), skip token verification
    # and rely on timestamps from the license.json written by activate/heartbeat.
    has_secret = _get_signing_secret() is not None
    if has_secret and payload is None:
        return {"status": "needs_activation", "reason": "bad_token"}

    expires_at = ""
    if payload:
        expires_at = data.get("expires_at") or payload.get("expires_at") or ""
    else:
        expires_at = data.get("expires_at", "")

    exp_dt = _parse_iso(expires_at)
    now = datetime.now(timezone.utc)
    if exp_dt is not None and exp_dt < now:
        return {"status": "expired", "expires_at": expires_at}

    last_check_dt = _parse_iso(data.get("last_check", ""))
    days_since_check = (
        _days_between(now, last_check_dt) if last_check_dt else float("inf")
    )

    if days_since_check >= HEARTBEAT_INTERVAL_DAYS:
        ok, reason = heartbeat()
        if ok:
            data = load_license() or data
            expires_at = data.get("expires_at", expires_at)
            return {
                "status": "valid",
                "expires_at": expires_at,
                "customer": data.get("customer", ""),
            }
        if reason == "revoked":
            return {"status": "revoked"}
        if reason == "expired":
            return {"status": "expired", "expires_at": expires_at}
        if last_check_dt is None:
            return {"status": "needs_activation", "reason": "offline_too_long"}
        if days_since_check < OFFLINE_GRACE_DAYS:
            days_left = max(1, int(OFFLINE_GRACE_DAYS - days_since_check))
            return {
                "status": "grace",
                "days_left": days_left,
                "message": (
                    f"License couldn't verify online. "
                    f"{days_left} day{'s' if days_left != 1 else ''} of offline use remaining."
                ),
            }
        return {"status": "needs_activation", "reason": "offline_too_long"}

    return {
        "status": "valid",
        "expires_at": expires_at,
        "customer": data.get("customer", ""),
    }


# ── Run authorization ─────────────────────────────────────────────────────────
def authorize_run(asin_count: int = 0, pincode_count: int = 0) -> tuple[bool, str, str, str]:
    """Request server authorization before a scraping run.

    Returns (authorized, error_message, run_token, expires_at).
    On network failure, falls back to 24-hour cached authorization.
    """
    data = load_license()
    if not data:
        return False, "No license found. Please activate first.", "", ""

    key = data.get("key", "")
    machine_id = data.get("machine_id") or get_machine_id()

    body = {
        "key": key,
        "machine_id": machine_id,
        "asin_count": asin_count,
        "pincode_count": pincode_count,
        "app_version": APP_VERSION,
    }
    ok, resp, net_err = _post("/authorize-run", body)

    if ok:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["last_authorized_at"] = now
        save_license(data)
        return (
            True, "",
            resp.get("run_token", ""),
            resp.get("expires_at", ""),
        )

    if net_err:
        log.warning("authorize_run network error: %s", net_err)
        return _check_offline_grace(data)

    reason = (resp or {}).get("reason", "unknown")
    log.info("authorize_run rejected: %s", reason)
    return False, _friendly(reason, resp), "", ""


def _check_offline_grace(data: dict) -> tuple[bool, str, str, str]:
    """Allow the run if the last successful authorization was within 24 hours."""
    last_auth_str = data.get("last_authorized_at", "")
    last_auth_dt = _parse_iso(last_auth_str)
    if last_auth_dt is None:
        return (
            False,
            "Could not reach the license server. Please check your internet connection.",
            "", "",
        )

    now = datetime.now(timezone.utc)
    hours_since = (now - last_auth_dt).total_seconds() / 3600.0

    if hours_since < OFFLINE_GRACE_HOURS:
        hours_left = max(1, int(OFFLINE_GRACE_HOURS - hours_since))
        log.info("authorize_run: offline grace, %d hours remaining", hours_left)
        return True, "", "", ""

    return (
        False,
        "Could not reach the license server and offline grace period (24 hours) has expired. "
        "Please check your internet connection and try again.",
        "", "",
    )
