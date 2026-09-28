# Amazon Price Verification Tool

Upload a CSV/XLSX of ASIN + expected price + brand, get back an Excel
workbook — one sheet per brand, listing only the ASINs with a problem
(price mismatch, out of stock, unavailable, or not found) — ready to
forward straight to that brand's seller. Runs as a local Flask app;
nothing leaves the machine except requests to amazon.in.

This is a separate tool from the root repo's delivery-date scraper
(`scraper.py`/`gui.py`) — different problem (price verification vs. delivery
promises), different architecture (async plain-HTTP pipeline vs.
Selenium/undetected-chromedriver per-row). See the root `CLAUDE.md`'s
Folder Structure section for how the two coexist in this repo.

## What changed after talking to the vendor

The original build spec assumed pincode mattered and the output was a
generic Matched/Mismatched/OOS/Failed set of sheets. Direct vendor
feedback changed both:

- **Pincode is not a factor** — this catalog has the same price everywhere,
  so there is no delivery-location session to set. This removed the
  single biggest architectural risk from the original build (see "Before
  this pivot" below) — the tool now fetches product pages with a plain,
  anonymous HTTP client, no browser, no cookie jar, no session lifetime to
  manage.
- **Output is grouped by brand, issues only.** The vendor's actual workflow:
  tally live Amazon price against what they agreed with a seller (e.g.
  "Coco Blue"), and when it's wrong, email that seller the discrepancy. So
  the workbook has one sheet per brand with only the problem rows —
  correct prices aren't shown, because nobody needs to see them.
- **Flag threshold: more than ₹1 deviation.** Exact match isn't required;
  anything within ₹1 is left alone.
- **Seller name matters as much as price** — the report tells you *who* is
  showing the wrong price, since that's who gets the email.
- **Speed still matters**: their existing tool takes ~4 hours for 1,000
  ASINs. The async concurrent pipeline (unchanged by this pivot) targets
  well under that.

## Status: code-complete and packaged, NOT yet validated against live Amazon

Everything is implemented, packaged (Windows `.exe`, CI-built on every
push — see "Packaging" below), and covered by `price_verifier/tests/` (76
offline tests, no live network, no browser). Run the tests with:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r price_verifier/requirements.txt
python -m unittest discover -s price_verifier/tests -p "test_*.py"
```

What's not yet proven is whether the selectors in `fetcher/parser.py` — and
the plain-HTTP fetch itself — actually work against a live amazon.in
response. This was built in a sandboxed environment with outbound access
to amazon.in blocked at the network-policy level (confirmed via a direct
CONNECT test, not a code issue), so none of this could be tested live here.

**Before trusting this tool's output, run it against 20-30 real ASINs on a
machine with normal internet access and manually check the numbers.** This
is a much smaller ask than the original build's "Phase 0" spike — dropping
the pincode requirement means there's no session/cookie lifetime question
left to investigate, just: does the plain HTTP GET return the same HTML
shape the selectors expect (see "Known gaps" below), and does Amazon's
bot-defense allow a sustained run at the chosen concurrency.

What *has* been verified in this sandbox, despite no route to amazon.in:
the packaged `.exe` was built and run (as a Linux binary from the same
spec — PyInstaller doesn't cross-compile, so this doesn't prove the
Windows build works bit-for-bit, but it exercises the same import graph,
data bundling, and frozen-path logic) all the way through a real upload →
pipeline run → Excel download, including the actual network-failure path
(this sandbox's outbound block was classified correctly, retried per
policy, and landed in the "Could Not Verify" sheet exactly as designed —
see git history for the transcript). What that run could NOT exercise is
the one thing that matters most: whether `fetcher/parser.py`'s selectors
match what amazon.in actually returns.

### Known gaps

- **Selectors in `fetcher/parser.py`** — ported from `scraper.py`'s
  `extract_price`/`extract_mrp`/`extract_availability`/`extract_seller`
  (proven against live Amazon via Selenium's live DOM), adapted to read the
  same CSS-addressable nodes out of *static* HTML from a plain GET. Should
  work — `.a-offscreen` price spans and `#merchant-info` are server-rendered,
  not JS-injected — but unverified against a real response.
- **Bot-defense at the TLS/header level** — `httpx[http2]` is a real
  browser-like stack but doesn't impersonate Chrome's exact TLS
  fingerprint. If Amazon's bot-defense fingerprints at that layer, expect
  elevated block rates even on an otherwise well-formed request; the
  fallback is `curl_cffi` (impersonates Chrome's TLS handshake) — not
  wired in yet, add it to `fetcher/http_client.py` if a live test run
  shows unexpected block rates.
- **"Lowest price across all sellers"** (`config.DEFAULT_PRICE_SOURCE = "lowest"`)
  — not implemented. A product page only ever exposes the Buy Box price;
  this would need the separate `/gp/offer-listing/{asin}` page.
- **Pincode session bootstrap** (`fetcher/session_bootstrap.py`) — kept in
  the codebase but unused by the default flow now that pincode doesn't
  matter for this vendor, and explicitly excluded from the packaged build
  (see "Packaging" below). If a future batch genuinely needs a
  per-pincode price, this is where that logic would plug back in; it was
  validated in concept (ported from this repo's proven
  `scraper.py::set_pincode()`) but never exercised end-to-end.

## Open questions still pending team confirmation

| Question | Current default |
|---|---|
| Buy Box price vs. lowest across sellers | Buy Box (`config.DEFAULT_PRICE_SOURCE`) |
| Marketplace | amazon.in only (`config.MARKETPLACE_BASE_URL`) |

Resolved by the vendor: match tolerance is ±₹1 (flag anything more),
pincode does not need to be set per batch or per row, Windows only (no
macOS build), and `DEFAULT_CONCURRENCY = 15` is fine without a benchmark
for now.

## Packaging

`price_verifier_windows.spec` (repo root) builds a single-file
`PriceVerificationTool.exe` via PyInstaller — Windows only, per the
vendor. Two things worth knowing if you're touching this:

- **No Cython, no license gate.** Unlike the root `amazon_scraper_windows.spec`,
  this tool has neither an anti-reverse-engineering build step nor a
  license server to authorize against — the spec is a plain Analysis →
  PYZ → EXE onefile build.
- **selenium/undetected-chromedriver are excluded from the bundle** even
  though they're in `requirements.txt` (for `fetcher/session_bootstrap.py`,
  unused by default — see "Known gaps"). Bundling a real Chrome +
  chromedriver pair for a code path nothing calls would only bloat the
  exe; if that path becomes load-bearing later, packaging it is separate
  work.
- **Templates ship as Python source, not a data folder.** See
  `templates_inline.py` — this sidesteps the whole class of "Flask can't
  find its templates once frozen" bugs by reusing the same trick this
  repo's own `gui.py` already relies on (`render_template_string` /
  no file-based templates under PyInstaller), rather than inventing a new,
  unverified mechanism (bundling `templates/` as PyInstaller `datas` and
  computing a frozen-aware `template_folder`).
- **Data directory (SQLite DB, uploads, output) lives next to the .exe**
  when frozen (`config.py`'s `_get_base_dir()`), not inside the PyInstaller
  bundle — the bundle is either read-only in spirit (onedir) or a temp dir
  wiped after every run (onefile), so writing there would either fail or
  silently lose data between runs.

**CI**: `.github/workflows/price_verifier_build.yml` builds on
`windows-latest` on every push that touches `price_verifier/**` or the
spec (any branch, not just main — "ready right after commit"), running the
offline test suite as a gate before building, and uploads the `.exe` as a
downloadable workflow artifact. `build_price_verifier_windows.bat` is the
local equivalent.

**Verified in this sandbox** (Linux, so not a Windows-bit-for-bit proof,
but it does exercise the same PyInstaller Analysis/bundling logic — see
"Status" above): the spec builds cleanly, all hidden imports resolve
(including `selectolax`'s compiled extension and `certifi`'s CA bundle,
both easy to get wrong), and the frozen binary correctly serves the UI,
writes its SQLite DB next to itself, and produces a valid, correctly
per-brand-grouped `.xlsx` after a full upload → run → download cycle.
**Not verified**: an actual Windows build (needs a Windows runner — CI
will do this on the next push), and anything downstream of a real Amazon
response.

## Running it

From source:

```bash
pip install -r price_verifier/requirements.txt
python -m price_verifier.app
```

Or grab the built `.exe` from the latest successful run of
`.github/workflows/price_verifier_build.yml` (Actions tab → that workflow
→ latest run → Artifacts) and just double-click it — no Python install
needed on the target machine.

Either way, opens `http://127.0.0.1:5001/` in your browser. Upload a
CSV/XLSX with `asin`, `expected_price`, and `brand` columns
(case-insensitive, tolerant of extra whitespace), confirm the settings,
and it runs — no pincode prompt, no browser launch.

### Input format

| Column | Required | Notes |
|---|---|---|
| `asin` | Yes | 10 characters, starts with `B` |
| `expected_price` | Yes | Numeric; `₹`, commas, and whitespace are stripped automatically |
| `brand` | Yes | Groups the output into one sheet per brand |
| `pincode` | No | Accepted if present, never used |

### Output

- **Overview** — one row per brand: total ASINs, matched, mismatched, out
  of stock / unavailable / not found, could-not-verify, and an "issues"
  count.
- **One sheet per brand** — only rows with an issue. Columns: ASIN,
  Product Title, Seller, Expected Price, Amazon Price, MRP, Difference,
  Status, URL, Checked At. Price-mismatch rows are highlighted.
- **Could Not Verify** — ASINs that failed to scrape after retries (not a
  pricing issue, just "we couldn't check this one"), with the error reason.
- **Per-brand download** — the results page also links a small
  single-sheet workbook per brand (same columns, that brand only) sized
  to attach directly to an email, without extracting a sheet from the
  full workbook first.

## Layout

```
(repo root)
├── price_verifier_windows.spec      PyInstaller spec — Windows only, onefile
├── build_price_verifier_windows.bat Local build script (mirrors the CI workflow's steps)
└── .github/workflows/
    └── price_verifier_build.yml     Builds + tests on windows-latest, uploads .exe artifact on every push

price_verifier/
├── app.py                    Flask UI — upload/confirm/progress/results/history; frozen-build startup log + error dialog
├── config.py                 Defaults for run settings, fixed system limits, frozen-aware writable BASE_DIR
├── templates_inline.py       Jinja templates as Python source (DictLoader) — no templates/ data folder to bundle
├── fetcher/
│   ├── parser.py             ALL Amazon HTML selectors live here (one-file fix on layout drift)
│   ├── http_client.py        Async httpx client — build_anonymous_client() is the default fetch path
│   ├── session_bootstrap.py  Pincode session bootstrap — kept but unused by default, excluded from the packaged build
│   └── models.py             FetchResult / ParsedProduct (now carries mrp + seller)
├── pipeline/
│   ├── runner.py             classify() (status + retryable) + the per-row retry loop
│   ├── compare.py            Pure tolerance-matching logic (±₹1 default)
│   ├── circuit_breaker.py    Consecutive-failure trip -> cooldown + concurrency cut
│   └── worker_pool.py        DynamicGate (resizable concurrency) + PauseGate (cooldown)
├── storage/
│   ├── db.py                 SQLite schema (WAL mode) — run_items carries brand/mrp/seller
│   └── checkpoint.py         Per-row write-through, resume query, run counts, brand-issue lookup
├── excel/report.py           build_report() (full, per-brand sheets) + build_brand_report() (single-brand email export); sanitizes cell text against formula injection
├── ingest/input_parser.py    CSV/XLSX upload validation (asin, expected_price, brand required)
└── tests/                    77 offline tests — fixtures, no network, no browser
```
