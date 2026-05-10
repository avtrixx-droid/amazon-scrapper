# CLAUDE.md — AmazonScraper Project

> This file is the persistent context for AI-assisted development on this project.
> Read this before making any changes. Do NOT edit scraper.py without understanding the vendor constraints below.

---

## Project Purpose

Production-grade Amazon.in scraper built for a **non-technical vendor** (Lapcare brand).
The vendor edits only `config.py` and `asins.txt`. They double-click a run script and get an Excel report.
Everything must be crash-safe, human-readable in output, and never expose Python tracebacks.

---

## Folder Structure

```
AmazonScraper/
├── scraper.py           ← main script — vendor never touches this
├── config.py            ← vendor ONLY edits this file
├── asins.txt            ← vendor pastes ASINs here (one per line, # for comments)
├── pincodes.txt         ← vendor edits pincodes here
├── requirements.txt     ← Python dependencies
├── run_windows.bat      ← double-click to run on Windows
├── run_mac.sh           ← double-click to run on Mac (must stay chmod +x)
├── build_exe.bat        ← builds .exe for Windows delivery via PyInstaller
├── README.txt           ← vendor-facing instructions (do not make technical)
├── logs/                ← auto-created; scraper_<date>.log written here silently
├── output/              ← Excel files saved here (fallback if Desktop not writable)
└── progress/            ← auto-created; progress.json for resume state
```

**Never add new top-level files without updating this CLAUDE.md.**

---

## Tech Stack

| Library | Version | Purpose |
|---|---|---|
| undetected-chromedriver | 3.5.3 | Stealth Chrome automation (NOT regular selenium) |
| selenium | 4.15.0 | Browser control |
| openpyxl | 3.1.2 | Excel output |
| requests | 2.31.0 | HTTP utilities |
| smtplib | built-in | Email delivery |
| json | built-in | Progress state |
| logging | built-in | Background debug logs |
| pathlib | built-in | Cross-platform Desktop path detection |

**Do not switch to regular selenium or playwright.** undetected-chromedriver is required for anti-bot evasion.

---

## Critical Architecture Decisions

### Pincode Batching (do not change this)
Process ALL ASINs for pincode 1 → then all for pincode 2 → etc.
This results in only N_pincodes browser pincode changes (e.g. 6), not N_asins × N_pincodes (e.g. 3000).
Changing this to per-ASIN pincode switching would make the script ~10x slower and far less stable.

### Chrome Version Matching
undetected-chromedriver must be told the installed Chrome major version explicitly via `version_main=`.
Auto-detect the installed Chrome version on startup. Clear driver cache and retry if there's a version mismatch.
Known issue on this project: Chrome 147 vs ChromeDriver 148 mismatch caused failures — fixed by reading local Chrome version.

### Output Fallback
Primary output: user's Desktop (detected via pathlib).
Fallback: `AmazonScraper/output/` if Desktop is not writable (common on sandboxed/corporate Macs).
Always print the actual save path to the vendor in plain English.

### Error Handling Rules
- NEVER show a Python traceback to the vendor in the terminal.
- ALL exceptions must be caught, logged to `logs/`, and shown as a friendly one-liner.
- Progress must be saved to `progress/progress.json` before any exit (crash or graceful).

---

## Excel Output Schema

### Sheet 1 — "Results"

| Col | Field | Notes |
|---|---|---|
| A | Category | Vendor-provided (from asins.txt grouping or config) |
| B | Item Name | Product title from Amazon, whitespace-stripped |
| C | Lapcare Item Code | Vendor internal code (from asins.txt or config mapping) |
| D | ASIN Link | Clickable hyperlink: `https://www.amazon.in/dp/{ASIN}` |
| E | Current Price (₹) | Selling price, numeric, ₹ removed |
| F | MRP (₹) | Crossed-out price, numeric |
| G | Discount (%) | Calculated if not on page: `round((MRP-Price)/MRP*100)` |
| H | Product Ranking | Best Sellers Rank if available, else "N/A" |
| I | Availability | "In Stock" / "Out of Stock" / "Low Stock (X left)" |
| J | Seller | Seller name, default "Amazon" |
| K | Rating | Numeric only (e.g. "4.2") |
| L | Reviews | Count only, commas removed |
| M | Delivery Date | Date text only, e.g. "Tomorrow, 16 Jan" |
| N | Free Delivery | "Yes" / "No" / "N/A" |
| O | {Pincode} - {City} | One column per pincode (sub-columns for location-wise availability) |
| ... | Additional pincode columns | Repeat pattern for each pincode in config |
| Z | Scraped At | Datetime string, e.g. "15 Jan 2024, 09:00 PM" |

**Location-wise availability uses sub-columns**, one per pincode, with the city name in the header. Each cell shows availability + delivery date for that pincode.

### Sheet 2 — "Summary"
Auto-generated: total ASINs, combinations, success rate, OOS count, avg price, avg rating, scrape datetime, time taken.

### Sheet 3 — "Failed"
Columns: ASIN | Pincode | City | Failure Reason | Timestamp

### Formatting Rules
- Header row: dark blue background, white bold text, height 25
- Freeze top row and first column
- Auto-filter on all columns
- Alternate row colors: white (#FFFFFF) and light grey (#F7F7F7)
- Row color coding:
  - Out of Stock → `#FFE0E0` (light red)
  - In Stock + same/next day → `#E0FFE0` (light green)
  - In Stock + 2–3 days → `#FFFDE0` (light yellow)
  - In Stock + 4+ days → `#FFF0E0` (light orange)
  - Failed/Error → `#F0F0F0` (light grey)
- Rows grouped by ASIN, sorted alphabetically by city within each ASIN group

---

## Data Extraction — CSS Selectors

Use multiple selectors per field; fall through to next on failure; mark "Not Found" if all fail.

| Field | Primary Selector | Fallbacks |
|---|---|---|
| Product Name | `#productTitle` | `.product-title`, `h1.a-size-large` |
| MRP | `.a-price.a-text-price span.a-offscreen` | `#priceblock_ourprice`, `.basisPrice` |
| Price | `.a-price-whole` + `.a-price-fraction` | `#priceblock_dealprice`, `#priceblock_ourprice` |
| Availability | `#availability span` | `#outOfStock`, `.availRed`, `.availGreen`, Add to Cart button presence |
| Delivery Date | `#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE` | `#deliveryMessageMirWidget`, `.delivery-message`, `#ddmDeliveryMessage` |
| Free Delivery | Check delivery block for "FREE" / "free delivery" | — |
| Seller | `#merchant-info a` | `#sellerProfileTriggerId`, `.offer-display-feature-text` |
| Rating | `#acrPopover span.a-icon-alt` | `[data-hook="rating-out-of-text"]` |
| Reviews | `#acrCustomerReviewText` | `[data-hook="total-review-count"]` |
| Product Ranking | `#SalesRank`, `#productDetails_detailBullets_sections1` | Parse "Best Sellers Rank" text |

---

## Browser Stealth Settings

- Use `undetected_chromedriver.Chrome()` — never `selenium.webdriver.Chrome()`
- Random window size: 1024–1920px wide
- Random user agent from a pool of 10 real Chrome/Windows agents
- Disable webdriver flag and automation flags
- Language: `en-IN`, Timezone: `Asia/Kolkata`
- Auto-detect installed Chrome major version → pass as `version_main=` to avoid mismatch errors
- Clear cached driver and retry once if version mismatch detected on startup

---

## CAPTCHA Handling

- Detect CAPTCHA by checking page content for known CAPTCHA indicators
- On detection: pause 5 minutes, print vendor-friendly countdown message
- After pause: retry from same point
- If CAPTCHA persists after retry: save progress, exit with friendly message telling vendor to run again tomorrow or from a different network

---

## Progress & Resume

File: `progress/progress.json`
```json
{
  "last_completed_index": 49,
  "completed_combinations": [["B09XYZ123", "110001"], "..."],
  "timestamp": "2024-01-15T21:06:00"
}
```
- Save every 10 scrapes
- Save on Ctrl+C or any crash before exiting
- On next run, detect file → ask vendor "Resume? (Y/N)"
- On completion: delete progress.json for a fresh start next time

---

## Terminal Output Format

Per-row progress:
```
✅ [0047/3000] B09XYZ | Delhi     | ₹1,499 | Tomorrow | In Stock
❌ [0049/3000] B08ABC | Bangalore | Failed - Retrying...
```

Summary line (updates every 10 scrapes):
```
Progress: 49/3000 (1.6%) | Success: 48 | Failed: 1 | Elapsed: 6 min | Remaining: ~6h 12m
```

---

## Config Validation Rules (run on startup)

| Config Key | Validation |
|---|---|
| `SEND_EMAIL = True` | Warn and disable silently if `EMAIL_PASSWORD` is blank |
| `MIN_DELAY` | Must be ≥ 3; warn if lower, clamp to 3 |
| `PINCODES` | Must have at least 1 entry |
| `OUTPUT_FOLDER` | Test writeability; fallback to `output/` if Desktop fails |

---

## Command Line Arguments

| Argument | Behaviour |
|---|---|
| *(none)* | Normal full run |
| `--test` | Only scrape first 3 ASINs × first 2 pincodes (6 combinations). Print "TEST MODE" banner. |

---

## asins.txt Format

```
# Electronics
B09W9FND7M,Lapcare Webcam 720p,LC-WC-720

# Home Appliances
B08N5WRWNW,Lapcare USB Hub 4-Port,LC-HUB-4P
```
Format per line: `ASIN[,Item Name[,Lapcare Item Code]]`
Lines starting with `#` are category headers — use them to populate the Category column.
Blank lines are ignored. Invalid ASINs (not 10 chars starting with B) are skipped with a warning.

---

## Known Issues & Fixes Applied

| Issue | Root Cause | Fix |
|---|---|---|
| "Could not start browser" | ChromeDriver version ≠ Chrome version (e.g. 147 vs 148) | Auto-detect Chrome major version, pass `version_main=`, clear cache on mismatch |
| `run_mac.sh` runs from wrong dir | Script was called from `~` not script folder | Added `cd "$(dirname "$0")"` as first line of `run_mac.sh` |
| Desktop not writable | Sandboxed environments / corporate Macs | Fallback output to `AmazonScraper/output/` with friendly message |
| `requirements.txt` not found | Same as above — wrong working directory | Fixed by `cd` in run scripts |

---

## What NOT to Change Without Discussion

- The batch-by-pincode strategy in the scraping loop
- The vendor-facing error message wording (must stay non-technical)
- The `config.py` structure and comments (vendor reads this directly)
- The Excel column order without updating this CLAUDE.md
- `run_mac.sh` must always start with `cd "$(dirname "$0")"` to work when double-clicked

---

## Pending / Open Items

- [ ] Product Ranking extraction: implement BSR parsing from product detail bullets
- [ ] Location-wise availability sub-columns: pivot Excel so each pincode is a column group under one ASIN row
- [ ] Category and Lapcare Item Code: read from extended asins.txt format (`ASIN,Name,ItemCode`)
- [ ] ASIN Link column: render as clickable Excel hyperlink (use `openpyxl` `=HYPERLINK()` formula)
- [ ] Verify `run_mac.sh` opens correctly on macOS Ventura/Sonoma (right-click → Open With → Terminal)
