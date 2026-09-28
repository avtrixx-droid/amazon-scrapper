"""
report.py — builds the output workbook.

Rewritten per the vendor's actual workflow (not the original spec's generic
Matched/Mismatched/OOS/Failed sheets): they tally price against what a
specific seller (e.g. "Coco Blue") is showing on Amazon, and when it's
wrong, they email that seller the discrepancy. So the report is now:

- One sheet PER BRAND, since each brand's issues get emailed to a
  different seller relationship.
- Each brand sheet lists ONLY rows with an issue (mismatch beyond
  tolerance, out of stock, unavailable, or not found) — a correct price
  is not something anyone needs to see or forward.
- Columns center on what an email needs: ASIN, title, seller (who to
  email), expected vs. actual vs. MRP, and the ₹ difference.
- Rows that failed to scrape (couldn't be checked at all) are NOT a
  pricing issue to send anyone — they go in one "Could Not Verify" sheet
  instead, plus an "Overview" sheet up front for a quick per-brand count
  before drilling into any one brand's tab.

Reads directly from SQLite (storage/checkpoint.py) rather than taking an
in-memory result list, so "re-download" from History, and the per-brand
single-sheet export, are both just re-running this against a past run_id.
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from price_verifier import config
from price_verifier.storage import checkpoint

HEADER_FILL = PatternFill("solid", fgColor="003366")
HEADER_FONT = Font(color="FFFFFF", bold=True)
ALT_FILL = PatternFill("solid", fgColor="F7F7F7")
MISMATCH_FILL = PatternFill("solid", fgColor="FFE0E0")

_STATUS_DISPLAY = {
    checkpoint.STATUS_MISMATCHED: "PRICE MISMATCH",
    checkpoint.STATUS_OUT_OF_STOCK: "OUT_OF_STOCK",
    checkpoint.STATUS_UNAVAILABLE: "UNAVAILABLE",
    checkpoint.STATUS_NOT_FOUND: "NOT_FOUND",
}

_ISSUE_SHEET_HEADERS = [
    "asin", "product_title", "seller", "expected_price", "amazon_price",
    "mrp", "difference", "status", "url", "checked_at",
]

_INVALID_SHEET_CHARS = re.compile(r"[\\/?*\[\]:]")


def _sanitize_sheet_name(name: str, used: set[str]) -> str:
    """Excel sheet names: <=31 chars, no \\ / ? * [ ] : , must be unique
    within the workbook. Truncates and de-dupes with a numeric suffix."""
    cleaned = _INVALID_SHEET_CHARS.sub(" ", (name or "Unknown Brand").strip()) or "Unknown Brand"
    cleaned = cleaned[:31]
    candidate = cleaned
    n = 2
    while candidate.lower() in used:
        suffix = f" ({n})"
        candidate = cleaned[: 31 - len(suffix)] + suffix
        n += 1
    used.add(candidate.lower())
    return candidate


def _write_table(ws: Worksheet, headers: list[str], display_headers: list[str], rows: list[tuple]) -> None:
    ws.append(display_headers)
    for col in range(1, len(display_headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 25

    for i, row in enumerate(rows, start=2):
        ws.append(row)
        if i % 2 == 0:
            for col in range(1, len(display_headers) + 1):
                ws.cell(row=i, column=col).fill = ALT_FILL

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(display_headers))}{max(len(rows) + 1, 1)}"

    for col in range(1, len(display_headers) + 1):
        letter = get_column_letter(col)
        max_len = len(str(display_headers[col - 1]))
        for row in rows:
            val = row[col - 1] if col - 1 < len(row) else ""
            max_len = max(max_len, len(str(val)) if val is not None else 0)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 60)


def _issue_row(r: dict) -> tuple:
    expected = r["expected_price"]
    actual = r["actual_price"]
    diff = round(actual - expected, 2) if actual is not None else None
    return (
        r["asin"],
        r["product_title"],
        r["seller"],
        expected,
        actual,
        r["mrp"],
        diff,
        _STATUS_DISPLAY.get(r["status"], r["status"].upper()),
        r["url"],
        r["checked_at"],
    )


def _write_issue_sheet(ws: Worksheet, items: list[dict]) -> None:
    display_headers = [
        "ASIN", "Product Title", "Seller", "Expected Price (₹)", "Amazon Price (₹)",
        "MRP (₹)", "Difference (₹)", "Status", "URL", "Checked At",
    ]
    rows = [_issue_row(r) for r in items]
    _write_table(ws, _ISSUE_SHEET_HEADERS, display_headers, rows)
    # Highlight price-mismatch rows so the biggest problem type is visible
    # at a glance without reading the Status column.
    for i, r in enumerate(items, start=2):
        if r["status"] == checkpoint.STATUS_MISMATCHED:
            for col in range(1, len(display_headers) + 1):
                ws.cell(row=i, column=col).fill = MISMATCH_FILL


def _brand_issue_items(run_id: str, brand: str, db_path: Path) -> list[dict]:
    all_items = checkpoint.get_run_items(run_id, db_path=db_path)
    return [
        r for r in all_items
        if r["brand"] == brand and r["status"] in checkpoint.ISSUE_STATUSES
    ]


def build_report(run_id: str, output_path: Path | None = None, db_path: Path = config.DB_PATH) -> Path:
    run = checkpoint.get_run(run_id, db_path=db_path)
    if run is None:
        raise ValueError(f"Unknown run_id: {run_id}")

    items = checkpoint.get_run_items(run_id, db_path=db_path)
    brands = sorted({r["brand"] for r in items}, key=str.lower)
    failed = [r for r in items if r["status"] == checkpoint.STATUS_FAILED]

    wb = Workbook()

    # ── Overview: one row per brand, quick triage before opening any tab ──
    ws_overview = wb.active
    ws_overview.title = "Overview"
    overview_rows = []
    for brand in brands:
        brand_items = [r for r in items if r["brand"] == brand]
        matched = sum(1 for r in brand_items if r["status"] == checkpoint.STATUS_MATCHED)
        mismatched = sum(1 for r in brand_items if r["status"] == checkpoint.STATUS_MISMATCHED)
        oos = sum(1 for r in brand_items if r["status"] in (
            checkpoint.STATUS_OUT_OF_STOCK, checkpoint.STATUS_UNAVAILABLE, checkpoint.STATUS_NOT_FOUND
        ))
        brand_failed = sum(1 for r in brand_items if r["status"] == checkpoint.STATUS_FAILED)
        overview_rows.append((
            brand, len(brand_items), matched, mismatched, oos, brand_failed, mismatched + oos,
        ))
    _write_table(
        ws_overview,
        ["brand", "total", "matched", "mismatched", "oos", "failed", "issues"],
        ["Brand", "Total ASINs", "Matched", "Mismatched", "Out of Stock / Unavailable / Not Found", "Could Not Verify", "Issues (needs action)"],
        overview_rows,
    )

    # ── One sheet per brand, issues only ───────────────────────────────────
    used_sheet_names: set[str] = {"overview"}
    for brand in brands:
        issue_items = _brand_issue_items(run_id, brand, db_path)
        ws = wb.create_sheet(_sanitize_sheet_name(brand, used_sheet_names))
        _write_issue_sheet(ws, issue_items)

    # ── Could Not Verify: scrape failures, not a pricing issue per se ──────
    ws_failed = wb.create_sheet(_sanitize_sheet_name("Could Not Verify", used_sheet_names))
    _write_table(
        ws_failed,
        ["asin", "brand", "error_reason", "attempts", "last_attempt_at"],
        ["ASIN", "Brand", "Error Reason", "Attempts", "Last Attempt At"],
        [(r["asin"], r["brand"], r["error_reason"], r["attempts"], r["checked_at"]) for r in failed],
    )
    if failed or items:
        summary_row = ws_failed.max_row + 2
        ws_failed.cell(row=summary_row, column=1, value=(
            f"{len(items) - len(failed)} of {len(items)} processed successfully; {len(failed)} could not be verified."
        )).font = Font(italic=True)

    if output_path is None:
        output_path = config.OUTPUT_DIR / f"PriceVerification_{run_id}.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return output_path


def build_brand_report(run_id: str, brand: str, output_path: Path | None = None, db_path: Path = config.DB_PATH) -> Path:
    """A single brand's issue list as its own small workbook — meant to be
    attached to the email that goes to that brand's seller directly,
    without the vendor needing to extract a sheet from the full report."""
    run = checkpoint.get_run(run_id, db_path=db_path)
    if run is None:
        raise ValueError(f"Unknown run_id: {run_id}")

    issue_items = _brand_issue_items(run_id, brand, db_path)

    wb = Workbook()
    ws = wb.active
    used_sheet_names: set[str] = set()
    ws.title = _sanitize_sheet_name(brand, used_sheet_names)
    _write_issue_sheet(ws, issue_items)

    if output_path is None:
        safe_brand = _INVALID_SHEET_CHARS.sub("_", brand).strip().replace(" ", "_") or "brand"
        output_path = config.OUTPUT_DIR / f"PriceVerification_{run_id}_{safe_brand}.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return output_path
