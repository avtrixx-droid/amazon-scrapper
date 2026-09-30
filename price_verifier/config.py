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

import os
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
# PV_DATA_DIR relocates everything the app writes (tests / the simulator use
# it so a throwaway run never touches the real run history).
DATA_DIR = Path(os.environ["PV_DATA_DIR"]).resolve() if os.environ.get("PV_DATA_DIR") else BASE_DIR / "data"
DB_PATH = DATA_DIR / "price_verifier.db"
OUTPUT_DIR = DATA_DIR / "output"
UPLOAD_DIR = DATA_DIR / "uploads"
DEBUG_HTML_DIR = DATA_DIR / "debug_html"

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

# Env override exists only so the test suite / local simulator
# (price_verifier/tests/sim_amazon.py) can point a real run at a fake Amazon.
MARKETPLACE_BASE_URL = os.environ.get("PV_MARKETPLACE_BASE_URL", "https://www.amazon.in").rstrip("/")

# Confirmed by the vendor: the same price applies at every pincode, so no
# delivery-location session bootstrap is needed for a normal run. This also
# removes the biggest architectural risk from the original build (see
# README.md "Known gaps" history) — plain anonymous HTTP requests, no
# browser dependency, no cookie/session lifetime to manage.
PINCODE_IS_A_FACTOR = False

# ── Fetching ───────────────────────────────────────────────────────────────
REQUEST_TIMEOUT_SECONDS = 20.0

# ── Multi-pass pipeline (pipeline/runner.py) ───────────────────────────────
# A live 30-ASIN run showed Amazon soft-blocks an identity after a short
# burst, and that a blocked identity NEVER recovers by waiting — only a fresh
# identity (new cookie jar / fingerprint) does. So instead of a circuit
# breaker that cools down and retries on the same session, the pipeline
# paces requests with an AIMD rate limiter, rotates the identity once per
# block event, and escalates unresolved rows to slower / heavier passes:
#   pass 1 "fast"     — shared session, adaptive rate
#   pass 2 "recovery" — fresh session after a short pause, slow fixed rate
#   pass 3 "browser"  — real Chrome (undetected-chromedriver), sequential
# Rows still unresolved after all passes end FAILED and are retryable.

# Pass 1 — adaptive rate limiter (requests per second across all workers).
# Patient by default: the tool runs from ONE office internet connection, and
# every source on scraping Amazon without proxies says a single IP only
# gets a few requests per minute before it is pushed back. So it starts
# slow and speeds up only while Amazon keeps answering normally — a block
# costs far more time (pause + fresh session) than a gentle start does.
FAST_INITIAL_RPS = 0.5
FAST_MIN_RPS = 0.15
FAST_MAX_RPS = 2.0
RATE_INCREASE_STEP = 0.15          # additive increase ...
RATE_INCREASE_EVERY = 10           # ... after this many consecutive successes
RATE_JITTER_FRACTION = 0.3         # +/- spacing jitter so requests aren't metronomic
BLOCK_PAUSE_BASE_SECONDS = 20.0    # global pause after a block event (doubles on repeats) ...
BLOCK_PAUSE_MAX_SECONDS = 90.0     # ... capped here
BLOCK_ESCALATION_WINDOW_SECONDS = 120.0  # blocks closer together than this escalate the pause
BLOCK_DECAY_SUCCESSES = 25         # this many successes in a row step the escalation back down
MAX_ATTEMPTS_FAST = 2              # per row, in pass 1, before deferring to pass 2
FAST_ABORT_BLOCK_STREAK = 4        # consecutive block events with no success -> give up on pass 1
FAST_ABORT_ERROR_STREAK = 30       # consecutive failed requests of any kind (e.g. network down) -> same
RETRY_BACKOFF_SECONDS = 1.5        # pause before re-trying a non-block error (timeout, 5xx, odd page)

# Pass 2 — recovery: fresh identity, slow and steady.
RECOVERY_PAUSE_SECONDS = 45.0
RECOVERY_RPS = 0.25
RECOVERY_CONCURRENCY = 2
MAX_ATTEMPTS_RECOVERY = 2
RECOVERY_ABORT_BLOCK_STREAK = 3
RECOVERY_ABORT_ERROR_STREAK = 10

# Pass 3 — real Chrome fallback (fetcher/browser_fallback.py).
BROWSER_FALLBACK_ENABLED = True
BROWSER_HEADLESS = True
BROWSER_PAGE_TIMEOUT_SECONDS = 30
BROWSER_READY_TIMEOUT_SECONDS = 20.0   # wait for #productTitle / captcha / 404 markers
BROWSER_SETTLE_TIMEOUT_SECONDS = 6.0   # then wait this long for price / availability to render
BROWSER_GAP_MIN_SECONDS = 2.0
BROWSER_GAP_MAX_SECONDS = 4.0
BROWSER_BLOCK_PAUSE_SECONDS = 30.0     # pause before restarting Chrome after a captcha/block
BROWSER_ABORT_BLOCK_STREAK = 3         # consecutive rows Chrome can't settle -> stop, leave rest FAILED
BROWSER_START_TIMEOUT_SECONDS = 180.0  # start/restart incl. first-run ChromeDriver download; then give up
BROWSER_CALL_TIMEOUT_SECONDS = 120.0   # one page fetch in Chrome, worst case
# Optional explicit Chrome/Chromium binary (else auto-detected like scraper.py).
CHROME_BINARY = os.environ.get("PV_CHROME_BINARY") or None
UC_CACHE_DIR = DATA_DIR / "uc_cache"   # undetected-chromedriver's chromedriver download cache

# ── Input validation ───────────────────────────────────────────────────────
# brand is optional: rows without one are grouped under the brand scraped
# from the product page (see storage.checkpoint.effective_brand). pincode is
# accepted if present (for the vendor's own record-keeping) but never used,
# since price doesn't vary by pincode for this catalog.
REQUIRED_COLUMNS = ("asin", "expected_price")
OPTIONAL_COLUMNS = ("brand", "pincode")
ASIN_LENGTH = 10
