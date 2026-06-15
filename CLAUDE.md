# CLAUDE.md — AmazonScraper Project (Enterprise Edition)

> **AGENTIC INSTRUCTIONS — READ BEFORE EVERY CHANGE**
> This file is authoritative. If any code contradicts this file, fix the code, not this file.
> Never edit `scraper.py` without fully understanding ALL sections below.
> Never add files without updating the Folder Structure section.
> Never change behavior without checking the "What NOT to Change" section.

---

## Project Purpose

Production-grade Amazon.in scraper built for a **non-technical vendor** (Lapcare brand).
The vendor edits only `config.py` and `asins.txt`. They double-click a run script and get an Excel report.
Everything must be crash-safe, human-readable in output, and never expose Python tracebacks.

### Enterprise Quality Bar
- Zero memory leaks — Chrome temp dirs cleaned on every exit path
- Zero orphan processes — PID lock enforced, cleaned on any crash
- Deterministic output — same inputs must produce same Excel schema every time
- Auditable — every run produces a timestamped log and a timestamped Excel file
- Recoverable — any crash mid-run resumes from last checkpoint without data loss

---

## Folder Structure

```
AmazonScraper/
├── scraper.py                    ← core scraper engine — vendor never touches
├── gui.py                        ← Flask web UI — entry point for built app
├── license.py                    ← client license module (activation + server-side run auth)
├── config.py                     ← vendor ONLY edits this (CLI mode)
├── asins.txt                     ← vendor pastes ASINs (one per line, # for comments)
├── pincodes.txt                  ← vendor edits pincodes
├── requirements.txt              ← Python dependencies (includes psutil>=5.9.0)
├── run_windows.bat               ← double-click on Windows (launches GUI)
├── run_mac.sh                    ← double-click on Mac (launches GUI; must stay chmod +x)
├── build_exe.bat                 ← legacy build script (now delegates to spec)
├── build_windows.bat             ← full Windows build: venv + spec + dist assembly
├── build_mac.sh                  ← full macOS build: venv + spec + xattr + codesign
├── amazon_scraper_windows.spec   ← PyInstaller spec for Windows (single-folder exe)
├── amazon_scraper_mac.spec       ← PyInstaller spec for macOS (.app bundle)
├── README.txt                    ← vendor-facing, non-technical instructions
├── README_VENDOR_APP.txt         ← shipped with the built app
├── templates/                    ← Flask HTML templates for gui.py
├── setup_cython.py               ← Cython build: compiles license.py + scraper.py to native .so/.pyd
├── tests/                        ← unittest suite (Selenium-free parser tests)
│   ├── test_delivery_parser.py   ← 94 tests for the delivery priority resolver
│   └── test_license.py           ← license authorization + grace period tests
├── license_server/               ← Render-hosted Flask + PostgreSQL license service
│   ├── app.py                    ← server: /activate, /heartbeat, /authorize-run, /admin/*, /healthz
│   ├── issue_key.py              ← admin CLI: issue/list/extend/revoke/release-machine
│   ├── requirements.txt          ← server-side deps (flask, itsdangerous, gunicorn, requests)
│   ├── render.yaml               ← one-click Render Blueprint
│   └── README.md                 ← deploy + usage guide
├── logs/                         ← auto-created; scraper_YYYYMMDD_HHMMSS.log per run
├── output/                       ← timestamped Excel files: AmazonReport_YYYYMMDD_HHMMSS.xlsx
├── progress/                     ← progress.json for resume state
└── .scraper.lock                 ← PID lock file (auto-created/deleted; never commit)
```

**Never add top-level files without updating this section.**
**Never commit `.scraper.lock` — add it to `.gitignore`.**

---

## Tech Stack

| Library | Version | Purpose |
|---|---|---|
| undetected-chromedriver | 3.5.3 | Stealth Chrome automation |
| selenium | 4.15.0 | Browser control |
| openpyxl | 3.1.2 | Excel output |
| requests | 2.31.0 | HTTP utilities |
| psutil | 5.9.x | Process/PID management for lock file |
| smtplib | built-in | Email delivery |
| json | built-in | Progress state |
| logging | built-in | Background debug logs |
| pathlib | built-in | Cross-platform Desktop path detection |
| tempfile | built-in | Chrome temp dir management |
| atexit | built-in | Guaranteed cleanup registration |
| signal | built-in | Ctrl+C and SIGTERM handling |

**Do not switch to regular selenium or playwright.**
**Do not use threading inside a single process** — undetected-chromedriver is not thread-safe.

**Multiprocessing is allowed for per-pincode-subset parallelism.** `gui.py`
spawns `scraper.run_worker` via `multiprocessing.Process`; each worker has its
own Python interpreter, its own `Chrome` driver, and its own Chrome temp dir.
Workers communicate with the GUI through a `multiprocessing.Queue`. This is
safe because no driver is shared across processes. **Do NOT** introduce
`threading.Thread` or share a driver between threads.

---

## Critical Architecture Decisions

### Pincode Batching (DO NOT CHANGE)
Process ALL ASINs for pincode 1 → then all for pincode 2 → etc.
This results in only N_pincodes browser pincode changes (e.g. 6), not N_ASINs × N_pincodes (e.g. 3000).
Changing this to per-ASIN pincode switching would make the script ~10x slower and far less stable.

### Chrome Version Matching
undetected-chromedriver must be told the installed Chrome major version explicitly via `version_main=`.
Auto-detect the installed Chrome major version on startup. Clear driver cache and retry once on mismatch.
Known issue on this project: Chrome 147 vs ChromeDriver 148 mismatch — fixed by reading local Chrome version.

### Output File Naming (ALWAYS USE TIMESTAMPS)
Every run must produce a NEW file. Never overwrite previous output.
```python
from datetime import datetime
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
filename = f"AmazonReport_{timestamp}.xlsx"
```
Save to Desktop first (pathlib), fallback to `output/`. Always print the exact save path.

### Error Handling Rules
- NEVER show a Python traceback to the vendor in terminal.
- ALL exceptions must be caught, logged to `logs/`, shown as a friendly one-liner.
- Progress must be saved to `progress/progress.json` before any exit (crash or graceful).
- The `atexit` module must register cleanup so it runs even on unhandled exceptions.

---

## Known Issues — Root Causes & Required Fixes

### ISSUE 1: ~35 GB Disk Usage on Mac (CRITICAL — FIX FIRST)

**Root cause:** Chrome creates a new temp profile directory (`/var/folders/.../`) on every driver
instantiation. These are never cleaned up on crash or normal exit, accumulating across runs.

**Fix — mandatory temp dir lifecycle management:**
```python
import tempfile
import shutil
import atexit

CHROME_TEMP_DIR = None  # module-level so atexit can reach it

def create_driver():
    global CHROME_TEMP_DIR
    CHROME_TEMP_DIR = tempfile.mkdtemp(prefix="amzscraper_chrome_")
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={CHROME_TEMP_DIR}")
    # ... rest of options
    driver = uc.Chrome(options=options, version_main=get_chrome_version())
    return driver

def cleanup_chrome_temp():
    global CHROME_TEMP_DIR
    if CHROME_TEMP_DIR and os.path.exists(CHROME_TEMP_DIR):
        try:
            shutil.rmtree(CHROME_TEMP_DIR, ignore_errors=True)
        except Exception:
            pass  # Never raise during cleanup

atexit.register(cleanup_chrome_temp)
```

**Also register cleanup on signal handlers:**
```python
import signal

def handle_exit(signum, frame):
    save_progress()
    cleanup_chrome_temp()
    release_lock()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_exit)   # Ctrl+C
signal.signal(signal.SIGTERM, handle_exit)  # kill / system shutdown
```

**Old temp dirs (one-time cleanup):** Add a startup routine to delete any
`amzscraper_chrome_*` dirs older than 24 hours from the system temp directory.

---

### ISSUE 2: "Already Running" on Page Reload (PID Lock File)

**Root cause:** No lock file exists. Reloading the page or running the script twice starts a
second Chrome session, which conflicts with the first.

**Fix — PID lock file with stale lock detection:**
```python
import psutil

LOCK_FILE = Path(__file__).parent / ".scraper.lock"

def acquire_lock():
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if psutil.pid_exists(pid):
                # Real conflict — another instance is running
                print(f"\n⚠️  The scraper is already running (process {pid}).")
                print("   Close the other terminal window first, then try again.")
                sys.exit(1)
            else:
                # Stale lock from a previous crash — safe to remove
                LOCK_FILE.unlink()
        except (ValueError, OSError):
            LOCK_FILE.unlink()  # Corrupt lock file — remove it
    LOCK_FILE.write_text(str(os.getpid()))

def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError:
        pass

atexit.register(release_lock)
```

Call `acquire_lock()` as the very first action in `main()`, before any other setup.

---

### ISSUE 3: Delivery Date Incorrect / Missing — FIXED

**Root cause (original):** Amazon's buy box is a multi-row accordion — each row is a different
fulfillment channel (Amazon Now/ALM, Standard/MIR, Scheduled). The old code used a single
selector waterfall and returned the first match, which was always the Standard/MIR row. The
faster Amazon Now row (minutes/hours delivery) was never checked. Additionally, after changing
pincode, the delivery widget refreshes via Ajax — the old `time.sleep(1.5)` wasn't long enough,
so all pincodes showed the first pincode's delivery date (stale-data copy bug).

**Fix applied — `extract_all_delivery_options(driver, expected_pincode, logger)`:**
- Reads ALL fulfillment channels: Amazon Now (ALM), Standard (MIR), DEX attribute
- Verifies `#contextualIngressPtLabel_deliveryShortLine` shows the expected pincode BEFORE
  reading any delivery text — prevents the stale-data copy bug across pincodes
- Normalises all options to "minutes from now" and returns the earliest one
- Falls back to page source regex if CSS selectors all miss
- Output format: `"Amazon Now – 10 Min (Free)"` / `"Standard – Tomorrow, 14 May (Free)"`
- Returns `"Not Available"` for OOS items (never blank/None)

**Follow-up (SENIOR-FIX-TIERED, 2026-05-30):** Replaced the old "collect every
delivery-shaped string inside the buy box, sort by datetime, take the smallest" approach
with a **tiered, early-return** extractor that mirrors Amazon's own DOM hierarchy. The old
approach worked but was noisier (lots of regex passes, lots of dedup) and the output format
("Amazon Now – 10 Minutes (Free)" / "Standard – Tomorrow, 31 May (Free)") leaked channel
internals into the Excel column the vendor reads.

`extract_all_delivery_options` now runs four tiers, scoped to the buy box:

| Tier | Selector | Behaviour |
|---|---|---|
| 1 — Amazon Now | `[id*='alm-delivery' i], [id*='qcomBuyBox' i], [id*='almOfferDisplay' i]` + `img[alt*='Amazon Now' i]` ancestor | If ANY element has parseable text → return `Amazon Now – <duration>`. **Trust Amazon**, do not compare against Tier 2. |
| 2 — Standard | `[id*='DELIVERY_BLOCK' i], [id*='deliveryBlock' i], [id*='deliveryMessage' i], [id*='promiseMessage' i]` | Read all matches, parse every phrase inside (so #deliveryBlockMessage's PRIMARY + SECONDARY children are both captured), pick the **earliest** by datetime regardless of price. |
| 3 — Buy-box fallback | `buy_box.innerText` regex sweep | Only when Tiers 1+2 returned nothing. Logged as WARNING so the new container ID can be added to Tier 1/2. |
| 4 — Not Available | — | All tiers missed. HTML dumped to `logs/delivery_debug/` for forensics. |

CSS `[id*='…' i]` is **regex on IDs in CSS form**. The substring + case-insensitive flag
means new slot suffixes Amazon ships (a hypothetical `…_TERTIARY_DELIVERY_MESSAGE_LARGE`)
are picked up automatically — no code change. The constants live at
[scraper.py](AmazonScraper/scraper.py) as `_AMAZON_NOW_SELECTOR` and
`_STANDARD_DELIVERY_SELECTOR`.

**Output format** (user-confirmed, written to Excel col M `Earliest Delivery`):
- Tier 1 hit → `Amazon Now – 10 Minutes` / `Amazon Now – 2 Hours`
- Tier 2/3 hit → `Tomorrow, 31 May` / `Today` / `2 June` (no channel prefix, no `(Free)`)
- Tier 4 → `Not Available`

The price flag still lives in the separate `Free Delivery` column (col N).

The result dict now includes `"tier": "amazon_now" | "standard" | "fallback" | "none"` so
log analysis can confirm which tier won per scrape.

New helpers: `_clean_delivery_text`, `_build_amazon_now_display`, `_build_standard_display`,
`_extract_amazon_now`, `_extract_standard_delivery`, `_extract_buybox_fallback`. The pure
`extract_earliest_delivery_from_text` helper is kept — it's reused by Tier 2 (to enumerate
multiple phrases inside one container) and by Tier 3 (last-resort sweep), and remains
unit-testable without Selenium.

Test coverage in `tests/test_delivery_parser.py` (69 cases):
`NewDisplayBuilderTests`, `Tier1AmazonNowTests`, `Tier2StandardDeliveryTests`,
`Tier3FallbackTests`, `TieredFlowIntegrationTests` — plus all the previous parser /
buy-box / scoping coverage. Run: `python -m unittest tests.test_delivery_parser`.

**Follow-up (SENIOR-FIX-BUYBOX, 2026-05-30):** Even with all the parser fixes below, the
extractor was reading "today" / "tomorrow" / weekday names from anywhere on the page —
customer reviews ("today my package arrived"), product description ("Get yours today!"),
Q&A ("Will this arrive Monday?"), Frequently-Bought-Together, recommendation carousels.
Any of those would race the real delivery promise and could win the `datetime` sort.

**The contract is now: every delivery read must be scoped to the buy box.** The buy box is
the right-hand offer panel that holds Price + Availability + Quantity + Add to Cart + Buy Now
+ Delivery + Sold by + Ships from + Payment. `find_buy_box(driver, logger)` returns that
element; `extract_all_delivery_options` calls it first and then runs DEX / slot / wildcard /
container / HTML / innerText reads via `buy_box.find_elements(...)` so nothing outside the
panel is ever parsed.

`find_buy_box` ranks candidates by `_score_buy_box_candidate(el)` (0–6 from CTA presence,
price element, quantity, sold-by, delivery block, availability). It tries the most-specific
wrappers first (`#buybox`, `#desktop_buybox`, `#apex_desktop`, `#rightCol`,
`#centerCol_feature_div`, `#centerCol`) and accepts the highest-scoring candidate with score ≥ 2.
Below that threshold it returns the best guess plus a logged warning so the run continues
but the issue is visible. Returning `None` (no candidate at all) makes the extractor skip
the HTML/innerText fallbacks entirely rather than risk a false positive — better to report
"Not Available" than the wrong date.

`BuyBoxScoringTests`, `BuyBoxFinderTests`, and `FalsePositiveScopeTests` in the test suite
demonstrate this contract: feeding the parser unscoped page text *will* produce wrong
answers; feeding it only the buy-box subtree produces correct ones.

**Follow-up (BUG-FIX-AMZNOW, 2026-05-30):** On products where Amazon shows both an Amazon Now
buy-box row AND a Standard buy-box row in the new accordion, the scraper kept reporting the
Standard "Tomorrow, 31 May" promise instead of the faster "FREE delivery in 10 minutes" one.
Three independent reasons were dropping the Amazon Now candidate:

1. `_parse_delivery_phrase` only matched the full words "minute"/"hour" — "30 mins", "2 hrs",
   "15 min", "1 hr" all silently returned `None`, so Amazon Now widgets using the abbreviated
   forms produced zero candidates. Fixed by switching the duration regex to
   `\b(\d+)\s+(?:minutes?|mins?)\b` / `\b(\d+)\s+(?:hours?|hrs?)\b`.
2. The container sweep list only knew about the legacy `#alm-…` IDs. Amazon's new
   multi-offer accordion uses `#mbc`, `#newAccordionRow_0/1/2`, `#qcomBuyBoxRow_feature_div`,
   `#almOfferDisplay_feature_div`, `#promiseMessage_feature_div`,
   `#buybox-default_feature_div`. Added all of them to `_DELIVERY_CONTAINER_IDS`.
3. `wait_for_delivery_block` returned `True` the moment ANY one channel populated. If the
   Standard MIR slot filled first, the page-source snapshot was taken before the Amazon Now
   accordion row hydrated. Now: if a fast-channel container is present in the DOM, wait for it
   to be populated too (not just the standard MIR slot). Page-source scan is also no longer
   capped at the first 200 KB and a `document.body.innerText` fallback was added for pages
   where Amazon Now content only appears after React hydration.

Also fixed: `_MONTHS` only had 3-letter prefixes, so "June 2" / "Tuesday, June 4" failed the
date-precedence step and silently fell through to weekday inference (returning the wrong date).
Long-form month names are now matched.

Also fixed: "today" used to resolve to today-at-noon, which is in the past for any afternoon
scrape — that caused "today" to wrongly sort ahead of a same-day "10 minutes" candidate.
"today" now resolves to today-at-20:00 (end of business), preserving today < tomorrow ordering
while letting minutes/hours candidates win within the same day.

New pure-Python helper `extract_earliest_delivery_from_text(text, default_channel, now)` lives
in `scraper.py`. It is Selenium-free so the priority rules are unit-testable. Coverage in
`tests/test_delivery_parser.py` (49 cases — parser + channel inference + display + buy-box
scoring/finder + false-positive scoping) — run with
`python -m unittest tests.test_delivery_parser`.

---

### ISSUE 4: Slow Speed

**Root causes:**
1. Chrome running in visible (non-headless) mode loads fonts, images, ads — wasting time and RAM
2. `time.sleep()` with fixed waits doesn't adapt to page load speed
3. No page load timeout — hangs forever on network issues
4. Images and tracking scripts load unnecessarily

**Fix — headless Chrome with resource blocking:**
```python
def build_chrome_options(temp_dir):
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={temp_dir}")

    # Performance — headless saves 60–70% RAM and 30–40% time
    options.add_argument("--headless=new")          # Use new headless (Chrome 112+)
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # Memory limits
    options.add_argument("--memory-pressure-off")
    options.add_argument("--max_old_space_size=512")
    options.add_argument("--js-flags=--max-old-space-size=512")

    # Block unnecessary resources
    options.add_argument("--blink-settings=imagesEnabled=false")   # No images
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,       # Block images
        "profile.default_content_setting_values.notifications": 2,  # Block notifications
        "profile.managed_default_content_settings.media_stream": 2,
    })

    # Stealth — still needed in headless
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--window-size={random.randint(1280,1920)},{random.randint(800,1080)}")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
    options.add_argument("--lang=en-IN")

    return options
```

**Fix — set page load timeout:**
```python
driver.set_page_load_timeout(30)   # Give up after 30s, not forever
driver.set_script_timeout(15)
```

**Fix — adaptive waits instead of fixed sleep:**
```python
def wait_for_product_page(driver, asin, timeout=20):
    """Wait until #productTitle is visible — not a fixed sleep."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.ID, "productTitle"))
        )
        return True
    except Exception:
        return False
```

---

### ISSUE 5: Inconsistent Behavior

**Root causes:**
1. Fixed `time.sleep()` too short for slow network, too long for fast — causes both misses and lag
2. No page hash/fingerprint check — script may scrape a CAPTCHA page silently
3. Session occasionally expires mid-run causing silent failures
4. CSS selectors sometimes race with Ajax re-renders

**Fix — pre-scrape page validation:**
```python
def validate_page_is_product(driver, asin):
    """Return True only if this looks like a real product page, not CAPTCHA/404/robot-check."""
    url = driver.current_url
    source_sample = driver.page_source[:3000].lower()

    if "captcha" in source_sample or "robot check" in source_sample:
        return "CAPTCHA"
    if "page not found" in source_sample or "404" in url:
        return "NOT_FOUND"
    if asin.lower() not in source_sample:
        return "WRONG_PAGE"
    if "add to cart" not in source_sample and "out of stock" not in source_sample:
        return "INCOMPLETE_LOAD"
    return "OK"
```

**Fix — retry decorator with exponential back-off:**
```python
import functools
import time
import random

def retry(max_attempts=3, base_delay=5):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
                    logging.warning(f"Attempt {attempt+1} failed: {e}. Retrying in {delay:.1f}s")
                    time.sleep(delay)
        return wrapper
    return decorator

@retry(max_attempts=3, base_delay=5)
def scrape_asin(driver, asin, pincode):
    # ... scraping logic ...
```

**Fix — browser health check every 50 scrapes:**
```python
SCRAPE_COUNT = 0

def check_browser_health(driver):
    global SCRAPE_COUNT
    SCRAPE_COUNT += 1
    if SCRAPE_COUNT % 50 == 0:
        try:
            driver.current_url  # Throws if browser crashed
        except Exception:
            logging.warning("Browser unresponsive — restarting")
            return False
    return True
```

---

## Excel Output Schema

### Timestamped Output (REQUIRED)
```python
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
excel_filename = f"AmazonReport_{timestamp}.xlsx"
```
Never overwrite a previous file. One file = one run. Always print path at the end.

### Sheet 1 — "Results"

| Col | Field | Notes |
|---|---|---|
| A | Category | From `#` header lines in asins.txt |
| B | Item Name | Product title, whitespace-stripped |
| C | Lapcare Item Code | Vendor internal code from asins.txt |
| D | ASIN Link | Clickable: `=HYPERLINK("https://www.amazon.in/dp/{ASIN}","{ASIN}")` |
| E | Current Price (₹) | Selling price, numeric |
| F | MRP (₹) | Crossed-out price, numeric |
| G | Discount (%) | `round((MRP-Price)/MRP*100)` if not on page |
| H | Product Ranking | Best Sellers Rank text, else "N/A" |
| I | Availability | "In Stock" / "Out of Stock" / "Low Stock (X left)" |
| J | Seller | Seller name, default "Amazon" |
| K | Rating | Numeric only (e.g. "4.2") |
| L | Reviews | Count only, commas removed |
| M | Earliest Delivery | Earliest option across all fulfillment channels. Format: `"{Channel} – {Date/Time} ({Free/₹XX})"`. Examples: `"Amazon Now – 10 Min (Free)"`, `"Standard – Tomorrow, 14 May (Free)"`, `"Standard – Wednesday, 17 May (₹40)"`, `"Not Available"` for OOS. |
| N | Free Delivery | "Yes" / "No" / "N/A" |
| O+ | {Pincode} - {City} | One column per pincode — availability + delivery |
| Z | Scraped At | "15 Jan 2024, 09:00 PM" |

### Sheet 2 — "Summary"
Auto-generated: total ASINs, combinations, success rate, OOS count, avg price, avg rating,
scrape datetime, time taken, Chrome version used, script version.

### Sheet 3 — "Failed"
Columns: ASIN | Pincode | City | Failure Reason | Timestamp | Retry Count

### Formatting Rules
- Header row: dark blue background (#003366), white bold text, height 25
- Freeze top row and first column
- Auto-filter on all columns
- Alternate row colors: white (#FFFFFF) and light grey (#F7F7F7)
- Row color coding:
  - Out of Stock → `#FFE0E0`
  - In Stock + same/next day → `#E0FFE0`
  - In Stock + 2–3 days → `#FFFDE0`
  - In Stock + 4+ days → `#FFF0E0`
  - Failed/Error → `#F0F0F0`
- Rows grouped by ASIN, sorted alphabetically by city within each ASIN group
- Column D (ASIN Link) must be an actual clickable `=HYPERLINK(...)` formula, not plain text

---

## Data Extraction — CSS Selectors

Use multiple selectors per field in order. Mark "Not Found" only if all fail.

| Field | Primary Selector | Fallbacks |
|---|---|---|
| Product Name | `#productTitle` | `.product-title`, `h1.a-size-large` |
| MRP | `.a-price.a-text-price span.a-offscreen` | `#priceblock_ourprice`, `.basisPrice` |
| Price | `.a-price-whole` + `.a-price-fraction` | `#priceblock_dealprice`, `#corePrice_desktop` |
| Availability | `#availability span` | `#outOfStock`, `.availRed`, `.availGreen`, Add to Cart presence |
| Delivery Date | See ISSUE 3 above — use full selector waterfall with WebDriverWait | — |
| Free Delivery | Inspect delivery block for "FREE" / "₹0" / "free delivery" | — |
| Seller | `#merchant-info a` | `#sellerProfileTriggerId`, `.offer-display-feature-text` |
| Rating | `#acrPopover span.a-icon-alt` | `[data-hook="rating-out-of-text"]` |
| Reviews | `#acrCustomerReviewText` | `[data-hook="total-review-count"]` |
| Product Ranking | `#SalesRank` | Parse "Best Sellers Rank" from `#productDetails_detailBullets_sections1` |

**All extractions must be wrapped in try/except. Never let one field failure abort the row.**

---

## Browser Stealth Settings

- Use `undetected_chromedriver.Chrome()` — never `selenium.webdriver.Chrome()`
- Run `--headless=new` (see ISSUE 4 fix above) — confirmed compatible with UC 3.5.3
- Random window size: 1280–1920px wide, 800–1080px tall
- Random user agent from a pool of 10 real Chrome/Windows agents (update pool annually)
- Disable webdriver flag and automation flags
- Language: `en-IN`, Timezone: `Asia/Kolkata`
- Auto-detect installed Chrome major version → pass as `version_main=`
- Clear cached driver and retry once if version mismatch detected on startup
- Do NOT reuse a stale driver instance — create fresh driver after CAPTCHA pause

---

## CAPTCHA Handling

1. Detect CAPTCHA by checking page content for known indicators
2. Log the detection with URL and timestamp
3. Print a vendor-friendly countdown: `⏳ Amazon asked for verification. Waiting 5 min before retry... (4:30 remaining)`
4. After pause: retry from same position
5. If CAPTCHA persists after one retry: save progress, quit with message:
   `"Amazon has temporarily restricted access. Run again tomorrow, or from a different network."`
6. On CAPTCHA exit: clean up Chrome temp dir and release lock before exiting

---

## Progress & Resume

File: `progress/progress.json`
```json
{
  "version": 2,
  "run_id": "20240115_210000",
  "last_completed_index": 49,
  "completed_combinations": [["B09XYZ123", "110001"]],
  "timestamp": "2024-01-15T21:06:00",
  "total_combinations": 3000,
  "output_file": "AmazonReport_20240115_210000.xlsx"
}
```
- Save every 10 scrapes AND on pincode boundary (between pincode groups)
- Save on Ctrl+C or any crash (via signal handler and atexit)
- On next run: detect file → ask vendor `"Resume previous run? (Y/N)"`
  - Y → restore output_file path and continue writing to SAME Excel file
  - N → delete progress.json, start fresh with new timestamp
- On completion: delete progress.json, print final output path

---

## Terminal Output Format

**Startup banner:**
```
╔══════════════════════════════════════════════════════╗
║         AmazonScraper v2.0 — Lapcare Edition        ║
║  Chrome: 147  |  ASINs: 500  |  Pincodes: 6         ║
╚══════════════════════════════════════════════════════╝
📁 Output: /Users/vendor/Desktop/AmazonReport_20240115_210000.xlsx
```

**Per-row progress:**
```
✅ [0047/3000] B09XYZ | Delhi      | ₹1,499 | Tomorrow  | In Stock
❌ [0049/3000] B08ABC | Bangalore  | Failed - Retrying (1/3)...
⚠️  [0051/3000] B07DEF | Mumbai     | Delivery date not found
```

**Rolling summary (updates every 10 scrapes):**
```
Progress: 49/3000 (1.6%) | ✅ 48 | ❌ 1 | ⚠️ 2 | Elapsed: 6 min | Remaining: ~6h 12m
```

**Exit summary:**
```
══════════════════════════════════════
✅ Scraping complete!
   Total: 3000 | Success: 2987 | Failed: 13
   Time taken: 6h 14m
   📁 Saved to: /Users/vendor/Desktop/AmazonReport_20240115_210000.xlsx
══════════════════════════════════════
```

---

## Logging

- One log file per run: `logs/scraper_YYYYMMDD_HHMMSS.log`
- Log level: DEBUG to file, WARNING to console only
- Every exception must be logged with full traceback (log file only, never terminal)
- Log format: `2024-01-15 21:06:00 | DEBUG | scraper.py:214 | Fetching B09XYZ for 110001`
- Logs older than 30 days: auto-delete on startup (keep last 30 only)
- Log Chrome version, UC version, Python version at startup

---

## Config Validation Rules (run on startup, before browser launch)

| Config Key | Validation |
|---|---|
| `SEND_EMAIL = True` | Warn and disable silently if `EMAIL_PASSWORD` is blank |
| `MIN_DELAY` | Must be ≥ 3; warn if lower, clamp to 3 |
| `PINCODES` | Must have at least 1 entry |
| `OUTPUT_FOLDER` | Test writeability; fallback to `output/` if Desktop fails |
| `asins.txt` | Warn on invalid ASINs (not 10 chars, not starting with B); skip them |

---

## Command Line Arguments

| Argument | Behaviour |
|---|---|
| *(none)* | Normal full run |
| `--test` | First 3 ASINs × first 2 pincodes only. Print "⚠️  TEST MODE" banner. No email. |
| `--no-headless` | Run with visible Chrome window (for debugging CAPTCHA / selector issues) |
| `--reset` | Delete progress.json and start fresh (no prompt) |
| `--version` | Print versions of all dependencies and Chrome, then exit |

---

## asins.txt Format

```
# Electronics
B09W9FND7M,Lapcare Webcam 720p,LC-WC-720

# Home Appliances
B08N5WRWNW,Lapcare USB Hub 4-Port,LC-HUB-4P
```

Format: `ASIN[,Item Name[,Lapcare Item Code]]`
- Lines starting with `#` → Category headers (populate Column A)
- Blank lines → ignored
- Invalid ASINs (not 10 chars, not starting with B) → skipped with warning, logged

---

## License System

Online activation + server-authorized run gate. The desktop app is
useless until the user enters a valid key on first launch. Every scraping
run requires server authorization via `POST /authorize-run`.

### Anti-Reverse-Engineering Layers

1. **Cython compilation** — `license.py` and `scraper.py` are compiled to
   native C extensions (`.so`/`.pyd`) before PyInstaller runs. These cannot
   be decompiled back to Python source.
2. **No signing secret on client** — `AMZ_LICENSE_SECRET` is no longer baked
   into the build. The client cannot verify or forge tokens locally.
3. **Server-side run gating** — every scraping run calls `POST /authorize-run`
   on the server. The server validates the key, machine, and logs the run.
   Without server approval, the scraper won't start.

### Flow

1. User installs the app and launches it. `gui.py` calls
   `license.check_license_status()` before rendering `/`. No license file
   → redirected to `/activate`.
2. Activation page posts the key (`AMZ-XXXX-XXXX-XXXX-XXXX`) to
   `POST /activate` on the Render-hosted server. Server binds the key to
   `machine_id` (SHA-256 of platform UUID + MAC).
3. Client writes `license.json` to the user's app-data dir.
4. On every scraping run, `gui.py` calls `license.authorize_run()` which
   POSTs to `/authorize-run`. Server validates the key/machine and returns
   a short-lived run token (1 hour). On success, `last_authorized_at` is
   cached in `license.json`.
5. On network failure: if `last_authorized_at` is within 24 hours, the run
   is allowed (offline grace). Beyond 24 hours, the user must reconnect.
6. On revoke/expire: the server rejects the authorization and the client
   shows a friendly error.

### Enforcement model (what actually blocks misuse)

`/authorize-run` enforces, in order: **revoked** (hard kill — blocks ALL
machines on the key) → **expired** → **machine binding under max_machines**.

**Self-heal:** if the key is valid/non-revoked/non-expired but this machine
has no activation row (server DB reset, migration, or a client that only kept
its license file), `/authorize-run` re-binds the machine **provided it's under
`max_machines`**. This prevents locking out a legitimate paid user when the
server loses an activation row. `max_machines` is still enforced, so a new
(N+1)th machine is refused (`max_machines_reached`).

**Consequence for `release-machine`:** it frees a slot (to move a license to a
new machine) but is NOT a permanent per-machine block — a valid key under its
limit can reclaim a free slot by running again. The hard "stop misuse now"
control is **`revoke`** (kills the whole key); `unrevoke` re-enables it.

### Where keys live

- **Server-side database** (PostgreSQL on Render): `keys`, `activations`,
  and `runs` tables. The `runs` table logs every run authorization request.
- **Client-side state** (`license.json`): key, machine_id, customer,
  expires_at, last_check, last_authorized_at.

### Issuing keys

```bash
cd license_server
python issue_key.py issue --customer "Lapcare" --days 365 --machines 1
```

Other admin ops: `list`, `extend`, `revoke`, `unrevoke`, `release-machine`, `info`.
See `license_server/README.md` for the full guide.

### Secrets

- `LICENSE_SIGNING_SECRET` (server env): used by itsdangerous to sign tokens.
- `LICENSE_ADMIN_TOKEN` (server env): Bearer auth for `/admin/*` endpoints.
- `AMZ_LICENSE_SECRET` is **no longer required** on the client side.

### Free Render tier caveat

The free Render web service sleeps after 15 min idle. The 24-hour offline
grace and 35-second request timeout handle cold starts transparently.

---

## Known Issues & Fixes Applied

| Issue | Root Cause | Fix Applied |
|---|---|---|
| ~35 GB disk on Mac | Chrome temp dirs never cleaned up | `tempfile.mkdtemp` + `shutil.rmtree` in `atexit` + signal handlers |
| "Already running" on reload | No lock file | PID lock with stale-lock detection via `psutil` |
| Delivery date wrong/missing + copy bug across pincodes | (1) Only Standard/MIR row was read — Amazon Now (faster) never checked. (2) Stale Ajax data read immediately after pincode change — all pincodes showed same date. | `extract_all_delivery_options()` reads ALL fulfillment channels, picks earliest; verifies `#contextualIngressPtLabel_deliveryShortLine` shows new pincode before reading delivery |
| Slow speed | Headed mode + fixed sleeps + no timeout | `--headless=new` + block images + `set_page_load_timeout` + `WebDriverWait` |
| Inconsistent behavior | Fixed sleeps, no page validation, no retry | Pre-scrape page validator + `@retry` decorator + exponential back-off |
| ChromeDriver version mismatch | Chrome vs UC version drift | Auto-detect Chrome version, `version_main=`, clear cache on mismatch |
| `run_mac.sh` wrong directory | Called from `~` not script folder | `cd "$(dirname "$0")"` as first line |
| Desktop not writable | Sandboxed/corporate Macs | Fallback to `output/` with friendly message |
| Files overwritten between runs | No timestamp in filename | Timestamped filename: `AmazonReport_YYYYMMDD_HHMMSS.xlsx` |
| Logs accumulate forever | No cleanup | Auto-delete logs older than 30 days on startup |
| **Windows .exe crash: `ModuleNotFoundError: No module named 'psutil'`** | `psutil` and its platform backends not listed in `hiddenimports` in `amazon_scraper_windows.spec` — PyInstaller misses runtime-only imports | Added `psutil`, `psutil._psutil_windows`, `psutil._psutil_linux`, `psutil._psutil_osx`, `psutil._common` to `hiddenimports` in both `.spec` files; rewrote `build_exe.bat` to use `amazon_scraper_windows.spec` |
| **Mac error -47 ("application cannot be opened")** | Missing ad-hoc code signature — macOS 12+ (Monterey) and all Apple Silicon Macs require at least an ad-hoc signature even for local builds | Added `codesign --force --deep --sign - dist/AmazonScraper.app` step to `build_mac.sh` after `xattr -cr`; added vendor-friendly Gatekeeper instructions to `README.txt` |
| **Excel missing CLAUDE.md cols I/M/N** | `FIXED_HEADERS` omitted Availability, Earliest Delivery, Free Delivery — fastest date was only inlined into per-pincode columns | Added all three to `FIXED_HEADERS`; new `_pick_earliest_result()` selects the globally-earliest OK result per ASIN by parsing `delivery_date` through `_parse_delivery_phrase` |
| **Chrome memory creep over long runs** | `check_browser_health` was dead code (defined, never called) — driver lived for entire 8h+ run | Wired `check_browser_health` into both CLI loop and `run_worker`; returns `(is_alive, should_recycle)`; new `_recycle_driver()` helper quits + cleans temp dir + relaunches every 50 scrapes |
| **Stale driver reused after CAPTCHA** | Old code did `time.sleep(5*60)` then retried with the same driver, violating "create fresh driver after CAPTCHA pause" | `pause_for_captcha` now polls every 10s with a countdown; callers (`scrape_with_smart_retry` and `run_worker`) recycle the driver after the pause and re-set pincode |
| **CAPTCHA wait looked like a freeze (especially in GUI)** | Flat 5-min sleep blocked the worker's `msg_queue`, so the GUI progress strip went silent | `pause_for_captcha` takes an optional `msg_queue=` parameter; emits a "M:SS remaining" tick every 10s so the GUI keeps updating |
| **`detect_captcha` pulled full `page_source` on every scrape** | Full-DOM serialization × 3000 scrapes was a measurable hot-path cost | Replaced with URL + title + `find_elements` selector probes (`#captchacharacters`, `form[action='/errors/validateCaptcha']`); `validate_page_is_product` got the same treatment |
| **Replaced "collect & sort" with tiered early-return extractor** | Old approach ran 6+ regex passes inside the buy box, built a giant candidate list, deduped, sorted, picked the first. Lots of code surface, hard to reason about, output leaked channel internals (`Amazon Now – X (Free)` / `Standard – X (Free)`). | New tiered flow mirrors Amazon's DOM: Tier 1 = Amazon Now (`[id*='alm-delivery' i], [id*='qcomBuyBox' i], [id*='almOfferDisplay' i]`) — short-circuits if found; Tier 2 = Standard (`[id*='DELIVERY_BLOCK' i], [id*='deliveryBlock' i], [id*='deliveryMessage' i], [id*='promiseMessage' i]`) — earliest of all parsed phrases; Tier 3 = buy-box innerText regex (insurance, logged WARNING); Tier 4 = Not Available. Output format: `Amazon Now – 10 Minutes` / `Tomorrow, 31 May` / `Today` / `Not Available`. Free flag moved to its own column. Result dict carries `"tier"` for log analysis. |
| **Delivery date matched review / Q&A / description text ("today my package arrived" → wrong date)** | Whole-page `page_source` and `document.body.innerText` fallbacks were scanning the entire DOM, so any "today" / "tomorrow" / weekday word inside reviews, Q&A, description, or recommendation widgets became a delivery candidate and won the `datetime` sort. | New `find_buy_box(driver, logger)` anchors on the right-hand offer panel (price + CTA + quantity + sold-by + delivery, score ≥ 2 out of 6). All DEX / slot / wildcard / container / HTML / innerText reads in `extract_all_delivery_options` now run via `buy_box.find_elements(...)`. Whole-page fallbacks removed. `find_buy_box` returns `None` when no candidate is found — extractor then skips text-blob fallbacks rather than risk a false positive. |
| **Amazon Now / Quick Commerce promise overlooked on accordion buy-box** | (a) Duration regex matched only "minute"/"hour" so "30 mins"/"2 hrs" dropped silently. (b) Container list missed `#mbc`, `#newAccordionRow_*`, `#qcomBuyBoxRow_feature_div`, `#almOfferDisplay_feature_div` — Amazon's new multi-offer buy box. (c) `wait_for_delivery_block` returned on the first populated channel, so the scraper read the page-source snapshot before Amazon Now hydrated. (d) `_MONTHS` only had 3-letter prefixes so "June 2" failed. (e) "Today" resolved to today-noon (in the past after lunch), wrongly beating same-day "10 minutes" candidates. | (a) Duration regex now allows `mins?`/`hrs?`. (b) Container list extended. (c) Wait now blocks until fast-channel containers populate when present; page-source scan no longer capped at 200 KB; added `document.body.innerText` fallback. (d) Full month names added to `_MONTHS` and `_MONTH_PAT`. (e) "Today" resolves to today-at-20:00. New pure helper `extract_earliest_delivery_from_text` lets the priority rules be unit-tested (`tests/test_delivery_parser.py`, 42 cases). |
| **Anyone with the .exe could use it indefinitely** | No license gate — the build was distributed as a free-running .exe with no activation, no expiry, no kill switch. | Built `license_server/` (Flask + SQLite on Render): `/activate` binds key↔machine_id, `/heartbeat` re-validates weekly, admin CLI in `issue_key.py`. New client module `license.py` stores signed token in user's app-data dir, verifies offline via itsdangerous, 14-day grace if the server is unreachable. `gui.py` gate redirects to `/activate` before any worker spawns; new `/license-status` endpoint exposes status JSON. PyInstaller spec adds `license`, `winreg`, `itsdangerous.*` to `hiddenimports` and requires `AMZ_LICENSE_SECRET` in the build env. |

---

## Startup Sequence (in order)

1. Print version banner
2. Clean up stale Chrome temp dirs (older than 24h matching `amzscraper_chrome_*`)
3. Auto-delete logs older than 30 days
4. Acquire PID lock (`acquire_lock()`) — exit if already running
5. Validate `config.py` values
6. Parse and validate `asins.txt` + `pincodes.txt`
7. Detect installed Chrome major version
8. Check for `progress/progress.json` → offer resume
9. Determine output file path (Desktop or fallback), print it
10. Launch Chrome (headless, with managed temp dir)
11. Register `atexit` and signal handlers
12. Begin scraping

---

## Shutdown Sequence (any exit path)

1. Save `progress/progress.json`
2. Finalize and save Excel file (flush all pending rows)
3. Quit Chrome driver (`driver.quit()`)
4. Delete Chrome temp dir (`shutil.rmtree`)
5. Release PID lock (`LOCK_FILE.unlink()`)
6. Print exit summary to terminal
7. Log final stats

`atexit.register()` must cover steps 3–5. Signal handlers must call steps 1–6 then `sys.exit(0)`.

---

## Pending / Open Items

- [ ] Implement BSR (Best Sellers Rank) parsing from `#productDetails_detailBullets_sections1`
- [ ] Location-wise availability pivot: each pincode as a column group under one ASIN row
- [ ] ASIN hyperlink: use openpyxl `=HYPERLINK()` formula (not plain text)
- [ ] Verify `run_mac.sh` on macOS Ventura/Sonoma (right-click → Open With → Terminal)
- [ ] One-time cleanup script for existing ~35 GB Chrome temp dirs on vendor Mac
- [ ] Email attachment: gzip Excel if > 10 MB before sending
- [x] ~~`--no-headless` flag for vendor to run visible Chrome when debugging CAPTCHAs~~
- [ ] Replace placeholder `support@yourdomain.com` in the activation page (`ACTIVATE_HTML` in `gui.py`)
- [ ] Set `LICENSE_SERVER_URL` in `license.py` to the real Render URL after the first deploy
- [x] ~~Configure `AMZ_LICENSE_SECRET` in the CI/build environment~~ — no longer needed; server-side auth replaces local token verification
- [x] ~~Anti-reverse-engineering hardening~~ — Cython compilation + removed client secret + server-side run gating

---

## What NOT to Change Without Discussion

- The batch-by-pincode strategy in the scraping loop
- The vendor-facing error message wording (must stay non-technical)
- The `config.py` structure and comments (vendor reads this directly)
- The Excel column order without updating this CLAUDE.md
- `run_mac.sh` must always start with `cd "$(dirname "$0")"`
- The shutdown sequence order (Excel must be saved before Chrome quits)
- The PID lock acquire/release symmetry (must always release, even on crash)
- **Buy-box anchoring for delivery extraction.** Never reintroduce a
  whole-page `page_source` or `document.body.innerText` scan for delivery
  text — it was the root cause of the "today my package arrived" false
  positive. All delivery reads must go through `find_buy_box(driver, logger)`
  and use `buy_box.find_elements(...)`. If the buy box can't be found, return
  "Not Available" — never silently scan the rest of the page.
- **Tier 1 (Amazon Now) short-circuits Tier 2 (Standard) — do not compare.**
  When `_extract_amazon_now` returns non-None we trust Amazon's channel and
  skip Tier 2 entirely. Don't "defensively" parse both and pick the earlier —
  Amazon Now is always the fastest available for that pincode by definition,
  and adding the compare path obscures the logic.
- **Use `[id*='…' i]` substring selectors for Tier 1/2, not exact IDs.**
  Amazon ships new slot suffixes without notice; substring + case-insensitive
  matching picks them up automatically. Adding hardcoded IDs is a long-term
  liability. Add new patterns only if a new family of IDs appears (different
  prefix entirely), not for each new suffix.
- **The license check must run before any worker spawns.** The `authorize_run()`
  call in `/start` and `/retry` must happen BEFORE `multiprocessing.Process`
  spawn. The whole point is to refuse the run cheaply, not to refuse it after
  Chrome has already started. Never call license functions from inside `_poll`,
  `_worker_entry`, `_handle`, `_build_excel`, or `_retry_failures`.
- **Don't move `LICENSE_SERVER_URL` into a config file the vendor can edit.**
  The vendor must not be able to redirect the activation flow to a fake
  server. It lives as a constant in `license.py`, baked into the build.
- **Never re-introduce `AMZ_LICENSE_SECRET` on the client side.** The signing
  secret was removed to prevent reverse engineers from extracting it and
  forging tokens. All authorization must go through the server.
- **Never skip the Cython compilation step in build scripts.** The `.py` source
  files must NOT ship in the PyInstaller bundle. The build scripts compile to
  `.so`/`.pyd` first and PyInstaller bundles the native extensions.
