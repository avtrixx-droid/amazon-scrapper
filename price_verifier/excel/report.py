"""
report.py — builds the 5-sheet output workbook (spec section 6).

Reads directly from SQLite (storage/checkpoint.py) rather than taking an
in-memory result list, so "re-download the output workbook" from the
History screen (spec section 8) is just calling this again against a past
run_id — no separate export-time data structure to keep in sync with what
got checkpointed.
"""

from __future__ import annotations

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


def _write_sheet(ws: Worksheet, headers: list[str], rows: list[tuple]) -> None:
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 25

    for i, row in enumerate(rows, start=2):
        ws.append(row)
        if i % 2 == 0:
            for col in range(1, len(headers) + 1):
                ws.cell(row=i, column=col).fill = ALT_FILL

    ws.freeze_panes = "A2"
    if rows or headers:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows) + 1, 1)}"

    for col in range(1, len(headers) + 1):
        letter = get_column_letter(col)
        max_len = len(str(headers[col - 1]))
        for row in rows:
            val = row[col - 1] if col - 1 < len(row) else ""
            max_len = max(max_len, len(str(val)) if val is not None else 0)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 60)


def build_report(run_id: str, output_path: Path | None = None, db_path: Path = config.DB_PATH) -> Path:
    run = checkpoint.get_run(run_id, db_path=db_path)
    if run is None:
        raise ValueError(f"Unknown run_id: {run_id}")

    items = checkpoint.get_run_items(run_id, db_path=db_path)
    matched = [r for r in items if r["status"] == checkpoint.STATUS_MATCHED]
    mismatched = [r for r in items if r["status"] == checkpoint.STATUS_MISMATCHED]
    oos = [r for r in items if r["status"] in (
        checkpoint.STATUS_OUT_OF_STOCK, checkpoint.STATUS_UNAVAILABLE, checkpoint.STATUS_NOT_FOUND
    )]
    failed = [r for r in items if r["status"] == checkpoint.STATUS_FAILED]

    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Matched"
    _write_sheet(
        ws1,
        ["asin", "expected_price", "actual_price", "product_title", "url", "checked_at"],
        [(r["asin"], r["expected_price"], r["actual_price"], r["product_title"], r["url"], r["checked_at"]) for r in matched],
    )

    ws2 = wb.create_sheet("Mismatched")
    _write_sheet(
        ws2,
        ["asin", "expected_price", "actual_price", "difference", "product_title", "url", "checked_at"],
        [
            (
                r["asin"], r["expected_price"], r["actual_price"],
                round((r["actual_price"] or 0) - r["expected_price"], 2),
                r["product_title"], r["url"], r["checked_at"],
            )
            for r in mismatched
        ],
    )

    ws3 = wb.create_sheet("Out of Stock")
    def _oos_status(status: str) -> str:
        return {
            checkpoint.STATUS_OUT_OF_STOCK: "OUT_OF_STOCK",
            checkpoint.STATUS_UNAVAILABLE: "UNAVAILABLE",
            checkpoint.STATUS_NOT_FOUND: "NOT_FOUND",
        }.get(status, status.upper())
    _write_sheet(
        ws3,
        ["asin", "expected_price", "status", "product_title", "url", "checked_at"],
        [(r["asin"], r["expected_price"], _oos_status(r["status"]), r["product_title"], r["url"], r["checked_at"]) for r in oos],
    )

    ws4 = wb.create_sheet("Failed")
    _write_sheet(
        ws4,
        ["asin", "error_reason", "attempts", "last_attempt_at"],
        [(r["asin"], r["error_reason"], r["attempts"], r["checked_at"]) for r in failed],
    )
    summary_row = ws4.max_row + 2
    total_processed = len(items)
    ws4.cell(row=summary_row, column=1, value=(
        f"{total_processed - len(failed)} of {total_processed} processed successfully; {len(failed)} failed."
    )).font = Font(italic=True)

    ws5 = wb.create_sheet("Summary")
    duration = ""
    if run["started_at"] and run["finished_at"]:
        from datetime import datetime
        try:
            start = datetime.fromisoformat(run["started_at"])
            end = datetime.fromisoformat(run["finished_at"])
            duration = str(end - start)
        except ValueError:
            duration = ""
    summary_rows = [
        ("Run ID", run["run_id"]),
        ("Started At", run["started_at"]),
        ("Finished At", run["finished_at"]),
        ("Duration", duration),
        ("Input File", run["input_filename"]),
        ("Pincode", run["pincode"]),
        ("Price Source", run["price_source"]),
        ("Tolerance (abs ₹)", run["tolerance_abs"]),
        ("Tolerance (%)", run["tolerance_pct"]),
        ("Concurrency", run["concurrency"]),
        ("Total Rows", run["total_rows"]),
        ("Matched", run["matched"]),
        ("Mismatched", run["mismatched"]),
        ("Out of Stock / Unavailable / Not Found", run["out_of_stock"]),
        ("Failed", run["failed"]),
    ]
    _write_sheet(ws5, ["Field", "Value"], summary_rows)

    if output_path is None:
        output_path = config.OUTPUT_DIR / f"PriceVerification_{run_id}.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return output_path
