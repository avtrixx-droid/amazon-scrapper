"""
tests/test_license.py — Unit tests for the license module's authorization flow.

Tests cover:
  • authorize_run() with mocked HTTP responses (success, revoked, expired, network error)
  • 24-hour offline grace logic
  • check_license_status() without signing secret (new builds)
  • _get_signing_secret() returns None when unconfigured

Run:  python -m unittest tests.test_license -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import license as lic


class _TempLicenseDir:
    """Mixin: redirect license storage to a temp directory for each test."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="test_license_")
        self._orig_app_data_dir = lic._app_data_dir
        lic._app_data_dir = lambda: Path(self._tmpdir)

    def tearDown(self):
        lic._app_data_dir = self._orig_app_data_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_license(self, data: dict) -> None:
        path = Path(self._tmpdir) / lic.LICENSE_FILENAME
        path.write_text(json.dumps(data), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_ago(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_from_now(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── _get_signing_secret ───────────────────────────────────────────────────────

class SigningSecretTests(unittest.TestCase):

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_none_when_no_secret_configured(self):
        if "AMZ_LICENSE_SECRET" in os.environ:
            del os.environ["AMZ_LICENSE_SECRET"]
        result = lic._get_signing_secret()
        self.assertIsNone(result)

    @patch.dict(os.environ, {"AMZ_LICENSE_SECRET": "test-secret-123"})
    def test_returns_secret_from_env(self):
        result = lic._get_signing_secret()
        self.assertEqual(result, "test-secret-123")


class SerializerTests(unittest.TestCase):

    @patch.dict(os.environ, {}, clear=True)
    def test_serializer_returns_none_without_secret(self):
        if "AMZ_LICENSE_SECRET" in os.environ:
            del os.environ["AMZ_LICENSE_SECRET"]
        result = lic._serializer()
        self.assertIsNone(result)

    @patch.dict(os.environ, {}, clear=True)
    def test_verify_signed_token_returns_none_without_secret(self):
        if "AMZ_LICENSE_SECRET" in os.environ:
            del os.environ["AMZ_LICENSE_SECRET"]
        result = lic.verify_signed_token("some-token-value")
        self.assertIsNone(result)


# ── authorize_run ─────────────────────────────────────────────────────────────

class AuthorizeRunSuccessTests(_TempLicenseDir, unittest.TestCase):

    def test_returns_true_on_server_approval(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
            "last_check": _now_iso(),
        })

        mock_resp = {
            "ok": True,
            "run_token": "tok_abc123",
            "expires_at": _days_from_now(0),  # 1 hour from now
            "server_time": _now_iso(),
        }
        with patch.object(lic, "_post", return_value=(True, mock_resp, "")):
            ok, err, token, expires = lic.authorize_run(asin_count=10, pincode_count=3)

        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(token, "tok_abc123")

        reloaded = lic.load_license()
        self.assertIn("last_authorized_at", reloaded)

    def test_caches_last_authorized_at(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
        })

        mock_resp = {"ok": True, "run_token": "tok_x", "expires_at": "", "server_time": ""}
        with patch.object(lic, "_post", return_value=(True, mock_resp, "")):
            lic.authorize_run()

        data = lic.load_license()
        self.assertIsNotNone(data.get("last_authorized_at"))


class AuthorizeRunRejectionTests(_TempLicenseDir, unittest.TestCase):

    def test_revoked_key_returns_false(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
        })

        mock_resp = {"ok": False, "reason": "revoked"}
        with patch.object(lic, "_post", return_value=(False, mock_resp, "")):
            ok, err, _, _ = lic.authorize_run()

        self.assertFalse(ok)
        self.assertIn("revoked", err.lower())

    def test_expired_key_returns_false(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
        })

        mock_resp = {"ok": False, "reason": "expired"}
        with patch.object(lic, "_post", return_value=(False, mock_resp, "")):
            ok, err, _, _ = lic.authorize_run()

        self.assertFalse(ok)
        self.assertIn("expired", err.lower())

    def test_no_license_file_returns_false(self):
        ok, err, _, _ = lic.authorize_run()
        self.assertFalse(ok)
        self.assertIn("activate", err.lower())


# ── Offline grace ─────────────────────────────────────────────────────────────

class OfflineGraceTests(_TempLicenseDir, unittest.TestCase):

    def test_allows_run_within_24_hours(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
            "last_authorized_at": _hours_ago(12),
        })

        with patch.object(lic, "_post", return_value=(False, {}, "network: timeout")):
            ok, err, _, _ = lic.authorize_run()

        self.assertTrue(ok)

    def test_blocks_run_after_24_hours(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
            "last_authorized_at": _hours_ago(25),
        })

        with patch.object(lic, "_post", return_value=(False, {}, "network: timeout")):
            ok, err, _, _ = lic.authorize_run()

        self.assertFalse(ok)
        self.assertIn("24 hours", err)

    def test_blocks_run_when_never_authorized(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
        })

        with patch.object(lic, "_post", return_value=(False, {}, "network: DNS failure")):
            ok, err, _, _ = lic.authorize_run()

        self.assertFalse(ok)
        self.assertIn("internet", err.lower())


# ── check_license_status without signing secret ──────────────────────────────

class CheckStatusWithoutSecretTests(_TempLicenseDir, unittest.TestCase):

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_valid_for_recent_check(self):
        if "AMZ_LICENSE_SECRET" in os.environ:
            del os.environ["AMZ_LICENSE_SECRET"]
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
            "signed_token": "old-token-from-activation",
            "last_check": _now_iso(),
            "customer": "TestCo",
        })

        status = lic.check_license_status()
        self.assertEqual(status["status"], "valid")
        self.assertEqual(status["customer"], "TestCo")

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_needs_activation_for_no_license(self):
        if "AMZ_LICENSE_SECRET" in os.environ:
            del os.environ["AMZ_LICENSE_SECRET"]
        status = lic.check_license_status()
        self.assertEqual(status["status"], "needs_activation")

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_expired_for_past_expiry(self):
        if "AMZ_LICENSE_SECRET" in os.environ:
            del os.environ["AMZ_LICENSE_SECRET"]
        past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": past,
            "signed_token": "old-token",
            "last_check": _now_iso(),
        })

        status = lic.check_license_status()
        self.assertEqual(status["status"], "expired")


# ── activate ──────────────────────────────────────────────────────────────────

class ActivateTests(_TempLicenseDir, unittest.TestCase):

    def test_successful_activation(self):
        mock_resp = {
            "ok": True,
            "customer": "TestVendor",
            "expires_at": _days_from_now(365),
            "signed_token": "server-signed-token-xyz",
        }
        with patch.object(lic, "_post", return_value=(True, mock_resp, "")):
            ok, err = lic.activate("AMZ-TEST-1234-ABCD-5678")

        self.assertTrue(ok)
        self.assertEqual(err, "")

        data = lic.load_license()
        self.assertEqual(data["key"], "AMZ-TEST-1234-ABCD-5678")
        self.assertEqual(data["customer"], "TestVendor")

    def test_activation_with_invalid_key(self):
        mock_resp = {"ok": False, "reason": "key_not_found"}
        with patch.object(lic, "_post", return_value=(False, mock_resp, "")):
            ok, err = lic.activate("AMZ-INVALID-KEY")

        self.assertFalse(ok)
        self.assertIn("couldn't find", err.lower())

    def test_activation_network_error(self):
        with patch.object(lic, "_post", return_value=(False, {}, "network: timeout")):
            ok, err = lic.activate("AMZ-TEST-1234-ABCD-5678")

        self.assertFalse(ok)
        self.assertIn("internet", err.lower())

    def test_empty_key_rejected(self):
        ok, err = lic.activate("")
        self.assertFalse(ok)
        self.assertIn("enter", err.lower())


# ── heartbeat ─────────────────────────────────────────────────────────────────

class HeartbeatTests(_TempLicenseDir, unittest.TestCase):

    def test_successful_heartbeat_updates_license(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
            "expires_at": _days_from_now(30),
            "last_check": _hours_ago(200),
        })

        mock_resp = {
            "ok": True,
            "expires_at": _days_from_now(30),
            "signed_token": "new-signed-token",
            "server_time": _now_iso(),
        }
        with patch.object(lic, "_post", return_value=(True, mock_resp, "")):
            ok, reason = lic.heartbeat()

        self.assertTrue(ok)
        data = lic.load_license()
        self.assertEqual(data["signed_token"], "new-signed-token")

    def test_heartbeat_revoked(self):
        self._write_license({
            "key": "AMZ-TEST-1234-ABCD-5678",
            "machine_id": "abc123",
        })

        mock_resp = {"ok": False, "reason": "revoked"}
        with patch.object(lic, "_post", return_value=(False, mock_resp, "")):
            ok, reason = lic.heartbeat()

        self.assertFalse(ok)
        self.assertEqual(reason, "revoked")

    def test_heartbeat_no_license(self):
        ok, reason = lic.heartbeat()
        self.assertFalse(ok)
        self.assertEqual(reason, "no_license")


if __name__ == "__main__":
    unittest.main()
