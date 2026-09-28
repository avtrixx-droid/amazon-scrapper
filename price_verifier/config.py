"""
config.py — tunables for the Price Verification Tool.

Unlike the delivery-scraper's config.py (vendor-edited, CLI-only), these are
exposed as run-time settings in the upload form; this module only holds the
defaults those form fields fall back to, plus fixed system limits.

The values marked "PENDING CONFIRMATION" are best-guess defaults from the
build spec's open questions (see README.md). They are config, not hardcoded
logic, specifically so they can change without a code edit once the team
confirms the real answer.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _get_base_dir() -> Path:
    """Where the SQLite DB, uploads, and Excel output live.

    Mirrors gui.py's _get_base_dir(): under a normal `python -m
    price_verifier.app` run this is the package directory, but once
    PyInstaller freezes this into a Windows .exe (see
    price_verifier_windows.spec), `Path(__file__).parent` resolves inside
    the bundle — read-only in spirit, and for a onefile build literally a
    temp directory that's wiped after every run. Data would either fail to
    write or vanish between runs. When frozen, use the folder that holds
    the .exe instead — writable, and it's what the vendor already expects
    ("the app remembers my run history") since that's how the root
    scraper.py/gui.py app already behaves.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _get_base_dir()
DB_PATH = BASE_DIR / "data" / "price_verifier.db"
OUTPUT_DIR = BASE_DIR / "data" / "output"
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
DEBUG_HTML_DIR = BASE_DIR / "data" / "debug_html"

for d in (DB_PATH.parent, OUTPUT_DIR, UPLOAD_DIR, DEBUG_HTML_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Defaults for run-time settings (overridable per run via the upload form) ──
DEFAULT_CONCURRENCY = 15          # spec suggests tunable 10-20
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 40

DEFAULT_PRICE_SOURCE = "buybox"   # "buybox" | "lowest" — PENDING CONFIRMATION (spec Q2)

# Confirmed by the vendor: flag any deviation of more than ₹1. tolerance_abs
# is inclusive (diff <= 1.0 is NOT flagged, matching "more than 1 rupee").
DEFAULT_TOLERANCE_ABS = 1.0
DEFAULT_TOLERANCE_PCT = 0.0       # percent, applied in addition to abs if set

MARKETPLACE_BASE_URL = "https://www.amazon.in"  # PENDING CONFIRMATION (spec Q5, .in assumed)

# Confirmed by the vendor: the same price applies at every pincode, so no
# delivery-location session bootstrap is needed for a normal run. This also
# removes the biggest architectural risk from the original build (see
# README.md "Known gaps" history) — plain anonymous HTTP requests, no
# browser dependency, no cookie/session lifetime to manage.
PINCODE_IS_A_FACTOR = False

# ── Retry / resilience ─────────────────────────────────────────────────────
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 20.0

# Circuit breaker: consecutive failures across the whole run (not per-row)
# before concurrency is cut and a cooldown is triggered.
CIRCUIT_BREAKER_THRESHOLD = 8
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 60
CIRCUIT_BREAKER_MAX_COOLDOWN_SECONDS = 600

# ── Input validation ───────────────────────────────────────────────────────
# "brand" is required (not just optional) because the whole point of the
# report is one sheet per brand, issues only — see excel/report.py. pincode
# is accepted if present (for the vendor's own record-keeping) but never
# used, since price doesn't vary by pincode for this catalog.
REQUIRED_COLUMNS = ("asin", "expected_price", "brand")
OPTIONAL_COLUMNS = ("pincode",)
ASIN_LENGTH = 10
