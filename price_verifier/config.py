"""
config.py — tunables for the Price Verification Tool.

Unlike the delivery-scraper's config.py (vendor-edited, CLI-only), these are
exposed as run-time settings in the upload form; this module only holds the
defaults those form fields fall back to, plus fixed system limits.

The three values marked "PENDING CONFIRMATION" are best-guess defaults from
the build spec's open questions (see CLAUDE_PRICE_VERIFIER.md). They are
config, not hardcoded logic, specifically so they can change without a
code edit once the team confirms the real answer.
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
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
DEFAULT_TOLERANCE_ABS = 1.0       # rupees — PENDING CONFIRMATION (spec Q3)
DEFAULT_TOLERANCE_PCT = 0.0       # percent, applied in addition to abs if set

MARKETPLACE_BASE_URL = "https://www.amazon.in"  # PENDING CONFIRMATION (spec Q5, .in assumed)

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
REQUIRED_COLUMNS = ("asin", "expected_price")
OPTIONAL_COLUMNS = ("pincode",)
ASIN_LENGTH = 10
