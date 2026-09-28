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
        csv = b"asin,expected_price,brand\nB09W9FND7M,1499,Lapcare\nB08N5WRWNW,999.50,Lapcare\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.total_rows, 2)
        self.assertEqual(report.valid_rows, 2)
        self.assertEqual(report.invalid_rows, 0)
        self.assertEqual(report.valid[0].brand, "Lapcare")

    def test_missing_price_column_raises(self):
        csv = b"asin,price,brand\nB09W9FND7M,1499,Lapcare\n"
        with self.assertRaises(InputValidationError):
            parse_upload("batch.csv", csv)

    def test_missing_brand_column_raises(self):
        csv = b"asin,expected_price\nB09W9FND7M,1499\n"
        with self.assertRaises(InputValidationError) as ctx:
            parse_upload("batch.csv", csv)
        self.assertIn("brand", str(ctx.exception))

    def test_blank_brand_cell_flagged_invalid(self):
        csv = b"asin,expected_price,brand\nB09W9FND7M,1499,\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 0)
        self.assertEqual(report.invalid_rows, 1)
        self.assertIn("brand is required", report.invalid[0].reason)

    def test_case_insensitive_headers(self):
        csv = b"ASIN,Expected_Price,Brand\nB09W9FND7M,1499,Lapcare\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 1)

    def test_short_asin_flagged_invalid(self):
        csv = b"asin,expected_price,brand\nB09W9F,1499,Lapcare\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 0)
        self.assertEqual(report.invalid_rows, 1)
        self.assertIn("10 characters", report.invalid[0].reason)

    def test_non_b_asin_flagged_invalid(self):
        csv = b"asin,expected_price,brand\nX09W9FND7M,1499,Lapcare\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.invalid_rows, 1)
        self.assertIn("start with 'B'", report.invalid[0].reason)

    def test_price_with_currency_symbol_and_commas(self):
        # Comma inside the value must be quoted, same as Excel would when
        # exporting a "₹1,499.00"-formatted cell to CSV.
        csv = b'asin,expected_price,brand\nB09W9FND7M,"\xe2\x82\xb91,499.00",Lapcare\n'
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid[0].expected_price, 1499.0)

    def test_non_numeric_price_flagged_invalid(self):
        csv = b"asin,expected_price,brand\nB09W9FND7M,not-a-price,Lapcare\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.invalid_rows, 1)

    def test_blank_trailing_rows_ignored(self):
        csv = b"asin,expected_price,brand\nB09W9FND7M,1499,Lapcare\n,,\n\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.total_rows, 1)

    def test_empty_file_raises(self):
        with self.assertRaises(InputValidationError):
            parse_upload("batch.csv", b"")

    def test_unsupported_extension_raises(self):
        with self.assertRaises(InputValidationError):
            parse_upload("batch.txt", b"asin,expected_price,brand\nB09W9FND7M,1499,Lapcare\n")

    def test_optional_pincode_column_accepted_but_uninvolved(self):
        csv = b"asin,expected_price,brand,pincode\nB09W9FND7M,1499,Lapcare,110001\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid[0].pincode, "110001")
        self.assertEqual(report.valid[0].brand, "Lapcare")

    def test_brand_whitespace_stripped(self):
        csv = b"asin,expected_price,brand\nB09W9FND7M,1499,  Lapcare  \n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid[0].brand, "Lapcare")


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
            ["asin", "expected_price", "brand"],
            ["B09W9FND7M", 1499, "Lapcare"],
            ["B08N5WRWNW", 999.5, "Portronics"],
        ])
        report = parse_upload("batch.xlsx", data)
        self.assertEqual(report.valid_rows, 2)
        self.assertEqual({r.brand for r in report.valid}, {"Lapcare", "Portronics"})

    def test_xlsx_numeric_price_preserved(self):
        data = self._make_xlsx([["asin", "expected_price", "brand"], ["B09W9FND7M", 1499, "Lapcare"]])
        report = parse_upload("batch.xlsx", data)
        self.assertEqual(report.valid[0].expected_price, 1499.0)


if __name__ == "__main__":
    unittest.main()
