"""
input_parser.py — validate and parse the vendor's uploaded CSV/XLSX.

Per spec section 5: reject with a clear error if required columns are
missing, report a parsed-row count, and flag malformed ASINs before the run
starts rather than discovering them mid-scrape.

Column matching is case-insensitive and tolerant of surrounding whitespace
("ASIN ", "Asin") since this file comes from a non-technical user's
spreadsheet, not a machine-generated export.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook

from price_verifier import config

_ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")
_PRICE_STRIP_RE = re.compile(r"[₹$,\s]")


class InputValidationError(Exception):
    """Raised when the file is unusable before any row-level parsing —
    missing required columns, unreadable format, or zero data rows."""


@dataclass
class ParsedRow:
    row_number: int  # 1-based, matches what the vendor sees in Excel/CSV
    asin: str
    expected_price: Optional[float]
    pincode: Optional[str]
    valid: bool
    reason: Optional[str] = None  # populated when valid=False


@dataclass
class ParseReport:
    rows: list[ParsedRow] = field(default_factory=list)
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    columns_found: list[str] = field(default_factory=list)

    @property
    def valid(self) -> list[ParsedRow]:
        return [r for r in self.rows if r.valid]

    @property
    def invalid(self) -> list[ParsedRow]:
        return [r for r in self.rows if not r.valid]


def _normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).strip().lower())


def _find_column(headers: list[str], *candidates: str) -> Optional[int]:
    normalized = [_normalize_header(h) for h in headers]
    for cand in candidates:
        cand_n = _normalize_header(cand)
        if cand_n in normalized:
            return normalized.index(cand_n)
    return None


def _coerce_price(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = _PRICE_STRIP_RE.sub("", str(raw))
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _validate_asin(raw) -> tuple[Optional[str], Optional[str]]:
    """Returns (normalized_asin_or_None, error_reason_or_None)."""
    if raw is None:
        return None, "ASIN is blank"
    asin = str(raw).strip().upper()
    if not asin:
        return None, "ASIN is blank"
    if len(asin) != config.ASIN_LENGTH:
        return None, f"ASIN must be {config.ASIN_LENGTH} characters, got {len(asin)}"
    if not asin.startswith("B"):
        return None, "ASIN must start with 'B'"
    if not _ASIN_RE.match(asin):
        return None, "ASIN contains invalid characters"
    return asin, None


def _rows_from_csv(data: bytes) -> tuple[list[str], list[list]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)
    if not all_rows:
        return [], []
    return all_rows[0], all_rows[1:]


def _rows_from_xlsx(data: bytes) -> tuple[list[str], list[list]]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = list(next(rows_iter))
    except StopIteration:
        return [], []
    header = ["" if h is None else str(h) for h in header]
    data_rows = [list(r) for r in rows_iter]
    return header, data_rows


def parse_upload(filename: str, data: bytes) -> ParseReport:
    """Entry point. `filename` decides CSV vs XLSX parsing; raises
    InputValidationError for anything wrong enough that starting a run
    would be pointless (missing columns, empty file)."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        header, data_rows = _rows_from_csv(data)
    elif suffix in (".xlsx", ".xlsm"):
        header, data_rows = _rows_from_xlsx(data)
    else:
        raise InputValidationError(f"Unsupported file type '{suffix}'. Upload a .csv or .xlsx file.")

    if not header:
        raise InputValidationError("The file appears to be empty — no header row found.")

    asin_col = _find_column(header, "asin")
    # Deliberately NOT matching a bare "price" here — a vendor's sheet may
    # carry both an expected_price and an unrelated current/list "price"
    # column, and silently guessing wrong would produce confidently-wrong
    # mismatches rather than the clear rejection the spec asks for.
    price_col = _find_column(header, "expected_price", "expectedprice", "expected price")
    pincode_col = _find_column(header, "pincode", "pin code", "pin")

    missing = []
    if asin_col is None:
        missing.append("asin")
    if price_col is None:
        missing.append("expected_price")
    if missing:
        raise InputValidationError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Found columns: {', '.join(h for h in header if str(h).strip())}"
        )

    # Drop fully-blank trailing rows (common in spreadsheet exports).
    data_rows = [r for r in data_rows if any(c not in (None, "") for c in r)]

    report = ParseReport(total_rows=len(data_rows), columns_found=header)
    for i, raw_row in enumerate(data_rows, start=2):  # row 1 is the header
        def cell(idx: Optional[int]):
            return raw_row[idx] if idx is not None and idx < len(raw_row) else None

        asin, asin_err = _validate_asin(cell(asin_col))
        price = _coerce_price(cell(price_col))
        pincode_raw = cell(pincode_col)
        pincode = str(pincode_raw).strip() if pincode_raw not in (None, "") else None

        if asin_err:
            report.rows.append(ParsedRow(i, str(cell(asin_col) or ""), price, pincode, False, asin_err))
            continue
        if price is None:
            report.rows.append(ParsedRow(i, asin, None, pincode, False, "expected_price is missing or not numeric"))
            continue

        report.rows.append(ParsedRow(i, asin, price, pincode, True))

    report.valid_rows = len(report.valid)
    report.invalid_rows = len(report.invalid)
    return report
