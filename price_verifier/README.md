# Amazon Price Verification Tool

Upload a CSV/XLSX of ASIN + expected price, get back an Excel workbook
flagging matches, mismatches, out-of-stock items, and failures — built per
the spec in the project conversation this was implemented from. Runs as a
local Flask app; nothing leaves the machine except requests to amazon.in.

This is a separate tool from the root repo's delivery-date scraper
(`scraper.py`/`gui.py`) — different problem (price verification vs. delivery
promises), different architecture (async plain-HTTP-first vs.
Selenium/undetected-chromedriver per-row). See the root `CLAUDE.md`'s
Folder Structure section for how the two coexist in this repo.

## Status: code-complete, NOT yet validated against live Amazon

Everything below the fetch boundary — CSV/XLSX ingest, SQLite
checkpoint/resume, the async concurrency + retry + circuit-breaker pipeline,
the 5-sheet Excel report, and the Flask UI — is implemented and covered by
`price_verifier/tests/` (51 tests, all offline, no network). Run them with:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r price_verifier/requirements.txt
python -m unittest discover -s price_verifier/tests -p "test_*.py"
```

What is **not** yet proven is the one thing the whole architecture hinges
on: whether Amazon's delivery-pincode cookies survive being replayed over
plain HTTP with no browser attached. This was built in a sandboxed
environment with outbound access to amazon.in blocked at the network-policy
level (confirmed via a direct CONNECT test, not a code issue), so that
question could not be tested here. **Do this first, on a machine with real
internet access, before trusting this tool's output:**

### Phase 0 — the pincode-over-HTTP spike (do this before anything else)

1. Install deps: `pip install -r price_verifier/requirements.txt`
2. Capture a session for a target pincode:
   ```bash
   python -m price_verifier.fetcher.session_bootstrap 110001 Delhi
   ```
   This opens a real (headless) Chrome via `undetected-chromedriver`, sets
   the pincode using the exact click sequence this repo's own
   `scraper.py::set_pincode()` already uses against live Amazon, and prints
   the captured cookie count + User-Agent.
3. Manually verify replay: use the cookies from step 2 in a plain `httpx`
   client (see `fetcher/http_client.py::build_client`), fetch 10-15 *different*
   ASINs' `/dp/{asin}` pages, and check `#contextualIngressPtLabel_deliveryShortLine`
   / the availability text in the returned HTML still reflects the pincode
   you set — not the site default.
4. Repeat after a 5-10 minute idle gap to check session lifetime against a
   batch that could run 20-40 minutes.
5. Record what you find in this file's "Known gaps" section below, and in
   the project's build plan. The outcome decides which of these three paths
   the tool actually needs:
   - **Cookies hold for the whole batch** → current architecture as-built is correct, no changes needed.
   - **Cookies degrade/expire mid-batch** → wire a re-bootstrap trigger into
     `pipeline/circuit_breaker.py`'s trip handler (currently it only cools
     down + shrinks concurrency; it would need to also call
     `session_bootstrap.bootstrap_session()` again and rebuild the httpx
     client with fresh cookies).
   - **Doesn't work at all** → replace `fetcher/http_client.py` with a
     Playwright-context pool (see the build-plan discussion for why that's
     the fallback, not undetected-chromedriver, for this tool specifically).

### Known gaps (things ported/designed from this repo's proven patterns, not independently verified here)

- **Selectors in `fetcher/parser.py`** — ported from `scraper.py`'s
  `extract_price`/`extract_mrp`/`extract_availability` (proven against live
  Amazon via Selenium's live DOM), adapted to read the same CSS-addressable
  nodes out of *static* HTML. Should work (`.a-offscreen` price spans are
  server-rendered, not JS-injected — confirmed by `scraper.py`'s own
  textContent-fallback logic needing to work even when `.text` is empty),
  but unverified against a real response.
- **"Lowest price across all sellers"** (`config.DEFAULT_PRICE_SOURCE = "lowest"`) —
  not implemented. A product page only ever exposes the Buy Box price; this
  needs the separate `/gp/offer-listing/{asin}` page, one extra request per
  ASIN. Currently the UI's price-source dropdown disables this option.
- **HTTP/2 + no TLS fingerprint impersonation** — `httpx[http2]` is a real
  browser-like stack but doesn't impersonate Chrome's exact TLS fingerprint.
  If Amazon's bot-defense fingerprints at that layer (plausible for
  Akamai/PerimeterX-style systems), you'll see elevated block rates even
  with valid cookies; the fallback is `curl_cffi` (impersonates Chrome's
  TLS handshake) — not wired in yet, add it to `http_client.py` if step 3
  above shows unexpected block rates.

## Open questions still pending team confirmation

These are wired as run-time settings (`config.py` defaults, overridable per
run in the confirm screen), not hardcoded, specifically so the answer can
change without a code edit:

| Question | Current default |
|---|---|
| Windows + Mac, or Windows only? | Not yet packaged either way — see "Not yet built" below |
| Buy Box price vs. lowest across sellers | Buy Box (`config.DEFAULT_PRICE_SOURCE`) |
| Match tolerance | ±₹1 absolute (`config.DEFAULT_TOLERANCE_ABS`), settable per run |
| Marketplace | amazon.in only (`config.MARKETPLACE_BASE_URL`) |

## Not yet built

- **PyInstaller packaging** (spec's step 9) — deliberately held until Phase
  0 confirms the architecture; packaging a tool whose core fetch strategy
  might still change is wasted effort. The root repo's
  `amazon_scraper_windows.spec` is the template to follow once this is
  ready — same `hiddenimports` discipline (see that spec's psutil scar
  tissue in the root `CLAUDE.md`).
- **50-ASIN throughput benchmark** (spec's step 3) — can't be run until
  Phase 0 passes; the concurrency defaults (`DEFAULT_CONCURRENCY = 15`) are
  the spec's suggested starting point, not a measured number.
- **Per-row pincode** — the spec flags batch-level pincode as materially
  cheaper; this tool only implements batch-level. If the team needs
  per-row, that's a real architecture change (re-bootstrapping the session
  per unique pincode in the batch), not a settings tweak.

## Running it

```bash
pip install -r price_verifier/requirements.txt
python -m price_verifier.app
```

Opens `http://127.0.0.1:5001/` in your browser. Upload a CSV/XLSX with
`asin` and `expected_price` columns (case-insensitive, tolerant of extra
whitespace), confirm the pincode + settings, and it runs.

## Layout

```
price_verifier/
├── app.py                    Flask UI — upload/confirm/progress/results/history
├── config.py                 Defaults for run settings + fixed system limits
├── fetcher/
│   ├── parser.py             ALL Amazon HTML selectors live here (one-file fix on layout drift)
│   ├── http_client.py        Async httpx client, one fetch attempt per call
│   ├── session_bootstrap.py  One-time browser session -> pincode + cookie jar (Phase 0)
│   └── models.py             FetchResult / ParsedProduct dataclasses
├── pipeline/
│   ├── runner.py             classify() (status + retryable) + the per-row retry loop
│   ├── compare.py            Pure tolerance-matching logic
│   ├── circuit_breaker.py    Consecutive-failure trip -> cooldown + concurrency cut
│   └── worker_pool.py        DynamicGate (resizable concurrency) + PauseGate (cooldown)
├── storage/
│   ├── db.py                 SQLite schema (WAL mode)
│   └── checkpoint.py         Per-row write-through, resume query, run counts
├── excel/report.py           5-sheet workbook builder (reads straight from SQLite)
├── ingest/input_parser.py    CSV/XLSX upload validation
├── templates/                upload, confirm, progress (SSE), results, history
└── tests/                    51 offline tests — fixtures, no network, no browser
```
