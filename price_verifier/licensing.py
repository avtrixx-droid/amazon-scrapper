"""
licensing.py — license client for the Price Verification Tool.

Uses the SAME license server and database as the Amazon Scraper
(license_server/, Flask on Render + Postgres on Supabase): same keys, same
admin CLI (license_server/issue_key.py), same machine binding. What makes it
a separate product is the `product` sent with every request — the server
only lets a key in if that product is in the key's product list (see
license_server/app.py "Products"). Give a customer access with:

    python issue_key.py issue --customer "X" --days 365 --products price_verifier
    python issue_key.py set-products --key AMZ-... --products amazon_scraper,price_verifier

`LicenseClient` is product-agnostic on purpose so the next tool can reuse it
(and the scraper's own license.py can move onto it later): pass a different
`product` and app-data folder name.

Behaviour mirrors license.py (the scraper's client):
  * activate(key) binds the key to this machine (same machine-id algorithm as
    license.py, so one key covering both tools on one PC uses ONE slot);
  * status() is the page gate: valid / grace / offline, or a reason to show
    the activation page (needs_activation / expired / revoked /
    product_not_licensed); heartbeats the server every 7 days;
  * authorize_run(n) is the hard gate before every run: the server checks
    key, expiry, product and machine and logs the run. If the server can't be
    reached, a run is still allowed within 24 hours of the last successful
    authorization (offline grace) — exactly like the scraper.

No signing secret is needed or used on the client.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

log = logging.getLogger("price_verifier.licensing")

PRODUCT = "price_verifier"
APP_DIR_NAME = "PriceVerificationTool"
APP_VERSION = "2.0"
# Same server as the scraper (license.py LICENSE_SERVER_URL). CI overrides it
# from the LICENSE_SERVER_URL repository secret via price_verifier/_build_config.py.
DEFAULT_SERVER_URL = "https://amazon-scraper-license.onrender.com"

HEARTBEAT_INTERVAL_DAYS = 7
OFFLINE_GRACE_HOURS = 24
REQUEST_TIMEOUT_SECONDS = 35   # Render's free tier can take ~30 s to wake up

# Reasons that mean "the license itself is the problem": send the user to the
# activation page rather than showing a transient error.
RELICENSE_REASONS = ("no_license", "key_not_found", "revoked", "expired", "max_machines_reached",
                     "product_not_licensed")
BLOCKING_STATUSES = ("needs_activation", "expired", "revoked", "product_not_licensed")

MESSAGES = {
    "no_license": "Please activate the Price Verification Tool with your license key.",
    "key_not_found": "We couldn't find that license key. Double-check the characters and try again.",
    "revoked": "This license has been revoked. Please contact support.",
    "expired": "This license has expired. Please contact support to renew it.",
    "max_machines_reached": ("This license is already in use on its maximum number of computers. "
                             "Contact support to move it to this computer."),
    "product_not_licensed": ("This license key doesn't include the Price Verification Tool. "
                             "Contact support to add it."),
    "bad_request": "The request was not understood. Please try again.",
    "network": "Could not reach the license server. Check your internet connection and try again.",
    "offline_expired": ("Could not reach the license server, and the 24-hour offline allowance has "
                        "run out. Connect to the internet and try again."),
}


def message_for(reason: str) -> str:
    if reason in MESSAGES:
        return MESSAGES[reason]
    if reason.startswith("network"):
        return MESSAGES["network"]
    return "The license check failed. Please try again or contact support."


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def server_url() -> str:
    """Baked into the .exe by CI; a dev run may override it with
    PV_LICENSE_SERVER_URL. A frozen build never reads the environment, so a
    user can't point the app at a fake license server."""
    try:
        from price_verifier._build_config import SERVER_URL  # type: ignore[import-not-found]
        if SERVER_URL:
            return SERVER_URL.rstrip("/")
    except ImportError:
        pass
    if not _is_frozen() and os.environ.get("PV_LICENSE_SERVER_URL"):
        return os.environ["PV_LICENSE_SERVER_URL"].rstrip("/")
    return DEFAULT_SERVER_URL


def enforced() -> bool:
    """Always on in the .exe. Developers running from source (tests, the
    local Amazon simulator) can switch it off with PV_LICENSE_DISABLED=1."""
    if _is_frozen():
        return True
    return os.environ.get("PV_LICENSE_DISABLED", "") != "1"


# ── Machine id — identical to license.py so one PC is one machine for all tools ──
def _read_windows_machine_guid() -> str:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value, _ = winreg.QueryValueEx(k, "MachineGuid")
            return str(value)
    except Exception:
        return ""


def _read_mac_platform_uuid() -> str:
    try:
        out = subprocess.check_output(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                      stderr=subprocess.DEVNULL, timeout=5).decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                return line.partition("=")[2].strip().strip('"')
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
    plat = sys.platform
    if plat.startswith("win"):
        platform_value = _read_windows_machine_guid()
    elif plat == "darwin":
        platform_value = _read_mac_platform_uuid()
    else:
        platform_value = _read_linux_machine_id()
    combined = f"{platform_value}|{format(uuid.getnode(), 'x')}|{plat}".encode("utf-8")
    return hashlib.sha256(combined).hexdigest()[:32]


def _app_data_dir(app_dir_name: str) -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app_dir_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_dir_name
    return Path.home() / ".config" / app_dir_name


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


@dataclass
class AuthResult:
    ok: bool
    reason: str = ""            # "" on success; one of RELICENSE_REASONS; "network" / "offline_expired"
    message: str = ""
    offline: bool = False       # allowed under the 24 h offline grace

    @property
    def relicense(self) -> bool:
        return self.reason in RELICENSE_REASONS


PostFn = Callable[[str, dict], "tuple[bool, dict, str]"]


class LicenseClient:
    """One product's license on this machine. `post` is a test hook with the
    same contract as _post(): (ok, response_json, network_error)."""

    def __init__(self, product: str = PRODUCT, app_dir_name: str = APP_DIR_NAME,
                 app_version: str = APP_VERSION, *, base_url: Optional[str] = None,
                 license_dir: Optional[Path] = None, post: Optional[PostFn] = None,
                 clock: Callable[[], datetime] = _now):
        self.product = product
        self.app_version = app_version
        self._base_url = base_url
        self._license_dir = Path(license_dir) if license_dir else None
        self._app_dir_name = app_dir_name
        self._post_fn = post
        self._now = clock

    # ── storage ────────────────────────────────────────────────────────────
    @property
    def license_path(self) -> Path:
        d = self._license_dir or _app_data_dir(self._app_dir_name)
        d.mkdir(parents=True, exist_ok=True)
        return d / "license.json"

    def load(self) -> dict | None:
        try:
            data = json.loads(self.license_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("key") else None
        except (OSError, ValueError):
            return None

    def _save(self, data: dict) -> None:
        path = self.license_path
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("could not save license file: %s", exc)

    def deactivate_local(self) -> None:
        """Forget the key on this machine (e.g. to enter a different one).
        Does not free the machine slot on the server — that is an admin
        action (issue_key.py release-machine)."""
        try:
            self.license_path.unlink()
        except OSError:
            pass

    # ── network ────────────────────────────────────────────────────────────
    def _post(self, path: str, body: dict) -> tuple[bool, dict, str]:
        body = dict(body, product=self.product, app_version=self.app_version)
        if self._post_fn is not None:
            return self._post_fn(path, body)
        url = (self._base_url or server_url()) + path
        try:
            r = httpx.post(url, json=body, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            return False, {}, f"network: {type(exc).__name__}"
        try:
            data = r.json()
        except ValueError:
            return False, {}, f"network: bad response (HTTP {r.status_code})"
        if r.is_success and isinstance(data, dict) and data.get("ok"):
            return True, data, ""
        return False, data if isinstance(data, dict) else {}, ""

    # ── public API ─────────────────────────────────────────────────────────
    def activate(self, key: str) -> AuthResult:
        key = (key or "").strip().upper()
        if not key:
            return AuthResult(False, "key_not_found", "Please enter your license key.")
        machine_id = get_machine_id()
        ok, data, net_err = self._post("/activate", {"key": key, "machine_id": machine_id})
        if not ok:
            reason = "network" if net_err else (data.get("reason") or "unknown")
            log.info("activation failed: %s %s", reason, net_err)
            return AuthResult(False, reason, message_for(reason))
        now = _iso(self._now())
        self._save({
            "key": key, "machine_id": machine_id, "product": self.product,
            "customer": data.get("customer", ""), "expires_at": data.get("expires_at", ""),
            "activated_at": now, "last_check": now, "last_authorized_at": "",
            "blocked_reason": "", "app_version": self.app_version,
        })
        return AuthResult(True)

    def heartbeat(self) -> AuthResult:
        data = self.load()
        if not data:
            return AuthResult(False, "no_license", message_for("no_license"))
        ok, resp, net_err = self._post("/heartbeat", {"key": data["key"],
                                                      "machine_id": data.get("machine_id") or get_machine_id()})
        if not ok:
            reason = "network" if net_err else (resp.get("reason") or "unknown")
            if reason in RELICENSE_REASONS:
                data["blocked_reason"] = reason
                self._save(data)
            return AuthResult(False, reason, message_for(reason))
        data.update(expires_at=resp.get("expires_at", data.get("expires_at", "")),
                    last_check=_iso(self._now()), blocked_reason="")
        self._save(data)
        return AuthResult(True)

    def status(self) -> dict:
        """For the page gate and the banner. {"status": ..., "reason"?,
        "message"?, "customer"?, "expires_at"?}. Never raises."""
        try:
            return self._status()
        except Exception:
            log.exception("license status check failed")
            return {"status": "offline", "message": "The license could not be checked just now."}

    def _status(self) -> dict:
        data = self.load()
        if not data or data.get("product", self.product) != self.product:
            return {"status": "needs_activation", "reason": "no_license"}
        blocked = data.get("blocked_reason") or ""
        if blocked in ("revoked", "expired", "product_not_licensed"):
            return {"status": blocked, "reason": blocked, "message": message_for(blocked)}
        if blocked:
            return {"status": "needs_activation", "reason": blocked, "message": message_for(blocked)}
        now = self._now()
        exp = _parse_iso(data.get("expires_at"))
        if exp is not None and exp < now:
            return {"status": "expired", "reason": "expired", "expires_at": data.get("expires_at"),
                    "message": message_for("expired")}
        info = {"customer": data.get("customer", ""), "expires_at": data.get("expires_at", "")}
        last = _parse_iso(data.get("last_check"))
        if last is None or (now - last).total_seconds() >= HEARTBEAT_INTERVAL_DAYS * 86400:
            hb = self.heartbeat()
            if hb.ok:
                fresh = self.load() or data
                return {"status": "valid", "customer": fresh.get("customer", ""),
                        "expires_at": fresh.get("expires_at", "")}
            if hb.reason in ("revoked", "expired", "product_not_licensed"):
                return {"status": hb.reason, "reason": hb.reason, "message": hb.message}
            if hb.reason in RELICENSE_REASONS:
                return {"status": "needs_activation", "reason": hb.reason, "message": hb.message}
            return dict(info, status="offline",
                        message="The license server couldn't be reached. Runs still need a connection "
                                "(or a successful check within the last 24 hours).")
        return dict(info, status="valid")

    def authorize_run(self, item_count: int) -> AuthResult:
        """The hard gate — call BEFORE starting any run (new, resume, retry)."""
        data = self.load()
        if not data or data.get("product", self.product) != self.product:
            return AuthResult(False, "no_license", message_for("no_license"))
        ok, resp, net_err = self._post("/authorize-run", {
            "key": data["key"], "machine_id": data.get("machine_id") or get_machine_id(),
            "asin_count": int(item_count), "pincode_count": 0,
        })
        if ok:
            data.update(last_authorized_at=_iso(self._now()), blocked_reason="")
            self._save(data)
            return AuthResult(True)
        if net_err:
            log.warning("authorize_run: license server unreachable (%s)", net_err)
            last = _parse_iso(data.get("last_authorized_at"))
            if last is not None and (self._now() - last).total_seconds() < OFFLINE_GRACE_HOURS * 3600:
                return AuthResult(True, offline=True)
            reason = "offline_expired" if last is not None else "network"
            return AuthResult(False, reason, message_for(reason))
        reason = resp.get("reason") or "unknown"
        log.info("authorize_run rejected: %s", reason)
        if reason in RELICENSE_REASONS:
            data["blocked_reason"] = reason
            self._save(data)
        return AuthResult(False, reason, message_for(reason))


_default: Optional[LicenseClient] = None


def client() -> LicenseClient:
    global _default
    if _default is None:
        _default = LicenseClient()
    return _default
