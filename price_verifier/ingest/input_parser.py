"""
input_parser.py — validate and parse the vendor's uploaded CSV/XLSX.

Per spec section 5: report a parsed-row count and flag malformed rows before
the run starts rather than discovering them mid-scrape.

The file comes from a non-technical vendor's own spreadsheet, not a
machine-generated export, so column names are NOT required to match
anything exact. The actual work — reading the file, finding the header row,
working out which column holds the ASIN / expected price / brand — lives in
`column_detect.py`. This module owns the result types (`ParsedRow`,
`ParseReport`, `InputValidationError`) that `app.py` and `column_detect.py`
both import, plus `parse_upload()`, a one-call convenience wrapper:

    load_table -> detect_columns -> parse_rows

A UI that wants the vendor to confirm/override the detected columns should
call those three `column_detect` functions directly instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class InputValidationError(Exception):
    """Raised when the file is unusable before any row-level parsing —
    unreadable format, no data rows, or no recognisable ASIN / expected
    price column. The message is shown to the vendor verbatim, so it must
    stay non-technical."""


@dataclass
class ParsedRow:
    row_number: int  # 1-based, matches what the vendor sees in Excel/CSV
    asin: str
    expected_price: Optional[float]
    brand: Optional[str]  # None when the file has no brand column / blank cell — valid
    pincode: Optional[str]  # accepted but never used — see config.PINCODE_IS_A_FACTOR
    valid: bool
    reason: Optional[str] = None  # populated when valid=False


@dataclass
class ParseReport:
    rows: list[ParsedRow] = field(default_factory=list)
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    columns_found: list[str] = field(default_factory=list)
    # Vendor-friendly notes from column detection (e.g. "we used 'MRP' as
    # the price column — please check"). Empty when parse_rows() is called
    # directly with a vendor-confirmed mapping.
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> list[ParsedRow]:
        return [r for r in self.rows if r.valid]

    @property
    def invalid(self) -> list[ParsedRow]:
        return [r for r in self.rows if not r.valid]


def parse_upload(filename: str, data: bytes) -> ParseReport:
    """Load, auto-detect columns and parse in one call.

    Raises InputValidationError for anything wrong enough that starting a
    run would be pointless: unreadable/empty file, or no column that looks
    like ASINs / expected prices. Everything row-level (bad ASIN, missing
    price, duplicate ASIN) is reported per-row in the returned ParseReport.
    """
    # Imported here, not at module top: column_detect imports this module's
    # dataclasses, so a top-level import would be circular.
    from price_verifier.ingest import column_detect

    table = column_detect.load_table(filename, data)
    detection = column_detect.detect_columns(table)
    column_detect.raise_if_unmapped(detection)
    report = column_detect.parse_rows(table, detection.mapping)
    report.warnings = list(detection.warnings)
    return report
