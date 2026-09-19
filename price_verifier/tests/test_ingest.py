"""Offline CSV/XLSX ingest validation tests. Run:
  python -m unittest price_verifier.tests.test_ingest
"""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from price_verifier.ingest.input_parser import InputValidationError, parse_upload


class CsvIngestTests(unittest.TestCase):
    def test_valid_rows_parsed(self):
        csv = b"asin,expected_price\nB09W9FND7M,1499\nB08N5WRWNW,999.50\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.total_rows, 2)
        self.assertEqual(report.valid_rows, 2)
        self.assertEqual(report.invalid_rows, 0)

    def test_missing_required_column_raises(self):
        csv = b"asin,price\nB09W9FND7M,1499\n"
        with self.assertRaises(InputValidationError):
            parse_upload("batch.csv", csv)

    def test_case_insensitive_headers(self):
        csv = b"ASIN,Expected_Price\nB09W9FND7M,1499\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 1)

    def test_short_asin_flagged_invalid(self):
        csv = b"asin,expected_price\nB09W9F,1499\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 0)
        self.assertEqual(report.invalid_rows, 1)
        self.assertIn("10 characters", report.invalid[0].reason)

    def test_non_b_asin_flagged_invalid(self):
        csv = b"asin,expected_price\nX09W9FND7M,1499\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.invalid_rows, 1)
        self.assertIn("start with 'B'", report.invalid[0].reason)

    def test_price_with_currency_symbol_and_commas(self):
        # Comma inside the value must be quoted, same as Excel would when
        # exporting a "₹1,499.00"-formatted cell to CSV.
        csv = b'asin,expected_price\nB09W9FND7M,"\xe2\x82\xb91,499.00"\n'
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid[0].expected_price, 1499.0)

    def test_non_numeric_price_flagged_invalid(self):
        csv = b"asin,expected_price\nB09W9FND7M,not-a-price\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.invalid_rows, 1)

    def test_blank_trailing_rows_ignored(self):
        csv = b"asin,expected_price\nB09W9FND7M,1499\n,,\n\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.total_rows, 1)

    def test_empty_file_raises(self):
        with self.assertRaises(InputValidationError):
            parse_upload("batch.csv", b"")

    def test_unsupported_extension_raises(self):
        with self.assertRaises(InputValidationError):
            parse_upload("batch.txt", b"asin,expected_price\nB09W9FND7M,1499\n")

    def test_optional_pincode_column(self):
        csv = b"asin,expected_price,pincode\nB09W9FND7M,1499,110001\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid[0].pincode, "110001")


class XlsxIngestTests(unittest.TestCase):
    def _make_xlsx(self, rows: list[list]) -> bytes:
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_valid_xlsx(self):
        data = self._make_xlsx([
            ["asin", "expected_price"],
            ["B09W9FND7M", 1499],
            ["B08N5WRWNW", 999.5],
        ])
        report = parse_upload("batch.xlsx", data)
        self.assertEqual(report.valid_rows, 2)

    def test_xlsx_numeric_price_preserved(self):
        data = self._make_xlsx([["asin", "expected_price"], ["B09W9FND7M", 1499]])
        report = parse_upload("batch.xlsx", data)
        self.assertEqual(report.valid[0].expected_price, 1499.0)


if __name__ == "__main__":
    unittest.main()
