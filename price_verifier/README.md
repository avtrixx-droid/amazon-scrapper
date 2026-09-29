# Amazon Price Verification Tool

Upload the vendor's CSV/XLSX price list, and get back an Excel workbook with
one sheet per brand. Each sheet lists only the ASINs that have a problem:
a price more than ₹1 off, out of stock, unavailable, no buy box, or not
found. It is ready to forward straight to that brand's seller.

It runs as a local Flask app (a double-click Windows `.exe`). Nothing leaves
the machine except requests to amazon.in.

This tool is separate from the root repo's delivery-date scraper
(`scraper.py`/`gui.py`). The rules in the root `CLAUDE.md` for that tool
(Selenium-only, pincode batching, the license system, Cython) do **not**
apply here.

## What the vendor asked for

- **Flag any deviation of more than ₹1.** Anything within ₹1 is left alone.
- **One sheet per brand, issues only.** Correct prices are not shown.
- **Columns:** ASIN → expected price → price shown on Amazon, plus MRP and
  seller, since the seller is who gets the email.
- **Pincode is not a factor.** The same price applies everywhere, so there
  is no delivery-location step.
- **Fast.** Their existing tool takes about 4 hours per 1,000 ASINs.

## v2 — fixes after the first live test (30 ASINs)

The first live run found three problems:

- the column names in the vendor's sheet were rejected;
- it took about 10 minutes for 30 ASINs;
- most rows failed.

The root cause of the failures and the slowness was the same. Amazon
soft-blocks a cookieless client whose TLS fingerprint is not a browser's.
The v1 retry loop then kept retrying on the same flagged identity, sleeping
between attempts, and never recovered. v2 fixes this at every layer.

### 1. Automatic column detection with a confirmation step (`ingest/column_detect.py`)

After upload, the tool works out which column is which. It uses the header
names (synonyms such as "ASIN No.", "Amazon Link", "SP", "Selling Price (Rs.)",
"Brand Name") and, above all, the **contents** of each column: which one
holds ASINs or amazon.in links, and which one holds prices. It also skips
title rows above the header, and picks the right sheet in a multi-sheet
workbook.

The **mapping screen** shows the guess, a preview of the file and a dropdown
for each field. Only after the user confirms does it show a summary (rows
ready, rows skipped and why), and only then does checking start. Details:

- ASINs can be given as bare ASINs or as any amazon.in product link.
- Prices can be written as `₹1,499`, `Rs. 1499/-`, `1499.00`, and so on.
- Duplicate ASINs are skipped, with the reason shown.
- The brand column is optional. Without one, the brand shown on Amazon is
  used for grouping.

### 2. Fetching like a real browser (`fetcher/http_client.py`)

`FetchSession` uses `curl_cffi` to impersonate Chrome's TLS and HTTP/2
fingerprint, so the request looks like a real browser to Amazon.

- **Warm-up:** each identity first loads the home page, the way a person
  would, and gets real session cookies. It also sets `i18n-prefs=INR` and
  `lc-acbin=en_IN`.
- **Rotation:** a blocked identity is never reused. `rotate()` switches to a
  fresh cookie jar and a fresh fingerprint.
- **Fallback:** httpx is used only if curl_cffi cannot load.

### 3. A three-pass pipeline that aims for zero failures (`pipeline/runner.py`)

| Pass | What | Why |
|---|---|---|
| 1. Fast | Shared session, requests paced by an **AIMD rate limiter** (`pipeline/rate_limiter.py`): it speeds up slowly while things go well, halves on a block, pauses globally, and rotates identity **once per block event** | Most rows finish here |
| 2. Recovery | A fresh identity after a short pause, at a slow fixed rate | Rows that were rate-limited in pass 1 |
| 3. Browser | Real Chrome through undetected-chromedriver, one row at a time (`fetcher/browser_fallback.py`) | Rows plain HTTP still could not settle (or ambiguous pages) |

Only rows that survive all three passes end as **Could Not Verify**. The
results page then shows a **Retry** button that re-checks just those rows
and updates the same report. A permanent answer is never re-fetched; that
includes matched, mismatched, out of stock, unavailable, not found and no
buy box.

Pass 3 needs Google Chrome installed on the machine. If Chrome is not there,
pass 3 is skipped, and those rows become retryable Could-Not-Verify rows
rather than wrong answers. The first Chrome run downloads a matching
chromedriver into `data/uc_cache`.

### 4. Parser accuracy (`fetcher/parser.py`)

The price and MRP are read only from the core price and buy-box containers,
never from anywhere on the page. That means carousel prices, "frequently
bought together" prices and review text cannot leak in. The parser also
reads the brand (from the byline) and the seller. It detects "no featured
offer" pages (a "See All Buying Options" page with no buy box) and gives
them their own status instead of reporting "no price".

When the parser cannot understand a page, it saves the page to
`data/debug_html/` so the problem can be investigated (pruned after 7 days).

### 5. Progress, pause and resume

The progress page shows the current pass, the live rate and an ETA. When
Amazon asks the tool to slow down, the page says so and counts down to
resuming.

**Pause** stops cleanly. Every finished row is already saved to SQLite (WAL
mode, write-through per row), and the home page offers **Resume**. A crash
or power cut is recovered the same way. Existing databases from v1 are
migrated in place; the migration only ever adds columns.

## Verification done so far

The build sandbox cannot reach amazon.in. Instead, `tests/sim_amazon.py` is
a local fake amazon.in that reproduces the failure seen in the live test.
Its throttle model:

- serves realistic 150–200 KB product pages, including decoy carousel,
  strike-through and review prices;
- flags an identity after a short burst and returns the 503 "Sorry!
  Something went wrong!" robot page to it from then on;
- enforces a per-IP ceiling across all identities.

Results against it:

| Check | Result |
|---|---|
| `test_e2e_sim` — 40 ASINs, realistic throttle, production request rates | **0 failed**, all prices, MRPs, sellers, brands and statuses correct; well under a minute |
| `test_e2e_sim` — 30 ASINs, harsh throttle (tiny bursts, 3 req/s IP ceiling) | **0 failed**; blocks happen and are absorbed by rotation and the recovery pass |
| Benchmark (`PV_SIM_BENCH=1`) — 300 ASINs, full production timings | **0 failed**, 147 s (about 120 ASINs/min, so about 8 min per 1,000; the vendor's tool takes 4 h) |
| Frozen PyInstaller binary: upload → auto-map → run → download | 40 ASINs in about 19 s, 0 failed; confirms curl_cffi is bundled correctly |
| Retry flow: an ASIN blocked permanently, then unblocked | Ends as Could Not Verify, then Retry resolves it and the report updates |

The real site's limits are unknown until the vendor's next live test. The
throttle model is an informed guess based on the first run. If the live
rate is lower, the AIMD limiter backs off on its own, and the recovery and
Chrome passes pick up the rest.

The whole suite (`tests/`, 348 tests) runs offline:

```bash
pip install -r price_verifier/requirements.txt
python -m unittest discover -s price_verifier/tests -t .
PV_SIM_BENCH=1 python -m unittest price_verifier.tests.test_e2e_sim   # + 300-ASIN benchmark
```

To try the full app against the simulator by hand:

```bash
python -m price_verifier.tests.sim_amazon --port 8765 --asins 300   # writes sim_batch.csv
PV_MARKETPLACE_BASE_URL=http://127.0.0.1:8765 PV_DATA_DIR=/tmp/pvdata python -m price_verifier.app
```

### Independent review

A separate reviewer pass then went through correctness, robustness,
Windows-specific behaviour and messy vendor sheets. Its findings are fixed,
and each one has a test in `tests/test_regressions.py`:

- **Price column choice.** An MRP column is no longer pre-selected over
  "Discount Price" or "Price (incl. GST)". GST-inclusive beats exclusive,
  and new/revised beats old.
- **Excel CSV exports.** A CSV exported from Excel whose "₹" became "?" is
  read correctly, and `.txt` "Unicode Text" exports are accepted.
- **The ₹1 boundary is exact.** It no longer suffers floating-point false
  flags.
- **The % tolerance** is now labelled as what it does: it ignores more
  differences.
- **Brand spelling.** "Lapcare" and "LAPCARE" become one sheet, one download
  and one email.
- **Chrome can't freeze a run.** A Chrome start that hangs (for example, a
  stalled ChromeDriver download) times out, and Pause always works.
- **Report files open in Excel.** A report file left open in Excel no longer
  turns a finished run into "crashed".
- **Resume/Retry.** They reuse the Chrome choice made at upload, and a
  double click cannot start two runs.
- **Disk use is bounded.** Debug pages are kept only for rows that could not
  be settled, they are capped at 200 MB, and they are pruned every run.
  Stale Chrome profiles are swept.

The Windows CI runner also exposed timing races in block handling that the
Linux sandbox hid. They are fixed, and `SlowDiskTests` reproduces them with
Windows-like disk latency.

### Known gaps

- **"Lowest price across all sellers"** (`DEFAULT_PRICE_SOURCE = "lowest"`)
  is not implemented. A product page only shows the Buy Box price, so this
  would need the `/gp/offer-listing/` page.
- **The Chrome pass has never run against a real Chrome here.** The
  sandbox's Chromium and chromedriver versions do not match. It is covered
  by tests using a fake driver, and it only handles rows the HTTP passes
  could not settle.

## Tunables

All tunables are in `config.py`:

- `DEFAULT_CONCURRENCY = 15`, which can be changed per run on the confirm
  screen;
- ₹1 tolerance;
- the rate-limiter and pass settings (`FAST_*`, `RATE_*`, `BLOCK_PAUSE_*`,
  `RECOVERY_*`, `BROWSER_*`).

Environment overrides, used for tests and the simulator:

- `PV_MARKETPLACE_BASE_URL`
- `PV_DATA_DIR`, which relocates the DB, uploads, output and debug pages
- `PV_CHROME_BINARY`

## Output workbook

- **Overview** has one row per brand: ASINs, Price OK, Price Mismatch,
  Listing Issue, Could Not Verify, and Needs Action.
- **One sheet per brand** holds that brand's issue rows only, with these
  columns:
  - ASIN
  - Expected Price (₹)
  - Price on Amazon (₹)
  - Difference (₹)
  - MRP (₹)
  - Seller
  - Product Title
  - Status
  - Note
  - Amazon Link (clickable)
  - Checked At
- **Could Not Verify** lists rows that could not be checked, with a plain
  English reason. Use Retry in the app to re-check them.
- **Run Info** shows how the run went: passes, blocks, the rate reached and
  the duration.
- **Per-brand download.** The results page also offers a single-brand
  workbook sized to attach to an email.

Cell text is sanitized against Excel formula injection.

## Packaging (Windows)

`price_verifier_windows.spec` (repo root) builds a single-file
`PriceVerificationTool.exe`:

- It collects curl_cffi (including its bundled libcurl-impersonate),
  selectolax's compiled parser, certifi, undetected-chromedriver and
  selenium.
- There is no Cython step and no license gate.
- Templates ship as Python source (`templates_inline.py`), so there is no
  template folder that a frozen build could fail to find.
- When frozen, the data directory is created next to the `.exe`.

**CI:** `.github/workflows/price_verifier_build.yml` runs on `windows-latest`
on every push that touches `price_verifier/**` or the spec, on any branch.
It runs the test suite as a gate, builds the `.exe`, and uploads it as the
`PriceVerificationTool-Windows` artifact. To download it: Actions tab →
latest run → Artifacts. `build_price_verifier_windows.bat` is the local
equivalent.

## Layout

```
price_verifier/
├── app.py                    Flask UI: upload → column mapping → confirm → progress (pause) → results (retry) → history
├── config.py                 Run defaults, pipeline tunables, frozen-aware data dir (+ PV_DATA_DIR)
├── templates_inline.py       Jinja templates as Python source (DictLoader)
├── ingest/
│   ├── column_detect.py      load_table / detect_columns / parse_rows — content + header based column detection
│   └── input_parser.py       ParsedRow / ParseReport / InputValidationError (+ parse_upload wrapper)
├── fetcher/
│   ├── http_client.py        FetchSession: curl_cffi Chrome impersonation, warm-up, rotate(); never raises
│   ├── browser_fallback.py   BrowserFetcher: real Chrome via undetected-chromedriver (pass 3)
│   ├── parser.py             ALL Amazon HTML selectors (scoped to price / buy-box containers)
│   ├── debug_dump.py         Saves unparseable pages to data/debug_html (7-day prune)
│   └── models.py             FetchResult / ParsedProduct
├── pipeline/
│   ├── runner.py             classify() + three-pass pipeline (fast / recovery / browser)
│   ├── rate_limiter.py       AdaptiveRateLimiter (AIMD + escalating block pauses)
│   └── compare.py            Tolerance matching (±₹1)
├── storage/
│   ├── db.py                 SQLite schema (WAL) + add-column-only migration
│   └── checkpoint.py         Per-row write-through, resume / pause / retry, per-brand issue counts
├── excel/report.py           Full report + single-brand export
└── tests/                    Offline suite + sim_amazon.py (fake amazon.in) + test_e2e_sim.py
```
