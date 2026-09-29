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

    def test_bare_price_header_accepted_with_warning(self):
        # A bare "price" heading used to be rejected outright; column
        # detection now accepts it (it's the only numeric column) but warns
        # so the vendor confirms it isn't the MRP.
        csv = b"asin,price,brand\nB09W9FND7M,1499,Lapcare\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 1)
        self.assertEqual(report.valid[0].expected_price, 1499.0)
        self.assertTrue(any("'price'" in w for w in report.warnings))

    def test_missing_price_column_raises(self):
        csv = b"asin,brand\nB09W9FND7M,Lapcare\n"
        with self.assertRaises(InputValidationError) as ctx:
            parse_upload("batch.csv", csv)
        self.assertIn("expected price", str(ctx.exception))

    def test_missing_asin_column_raises(self):
        csv = b"sku,expected_price,brand\nLC-01,1499,Lapcare\n"
        with self.assertRaises(InputValidationError) as ctx:
            parse_upload("batch.csv", csv)
        self.assertIn("ASIN", str(ctx.exception))

    def test_missing_brand_column_is_valid(self):
        # Brand is optional now — the run falls back to Amazon's brand.
        csv = b"asin,expected_price\nB09W9FND7M,1499\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 1)
        self.assertIsNone(report.valid[0].brand)
        self.assertTrue(any("No brand column" in w for w in report.warnings))

    def test_blank_brand_cell_is_valid(self):
        csv = b"asin,expected_price,brand\nB09W9FND7M,1499,\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 1)
        self.assertEqual(report.invalid_rows, 0)
        self.assertIsNone(report.valid[0].brand)

    def test_duplicate_asin_flagged_invalid(self):
        csv = b"asin,expected_price,brand\nB09W9FND7M,1499,Lapcare\nB09W9FND7M,1599,Lapcare\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid_rows, 1)
        self.assertEqual(report.invalid[0].row_number, 3)
        self.assertIn("Duplicate of row 2", report.invalid[0].reason)

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
        self.assertEqual(report.invalid[0].row_number, 2)

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

    def test_header_only_file_raises(self):
        with self.assertRaises(InputValidationError):
            parse_upload("batch.csv", b"asin,expected_price,brand\n")

    def test_optional_pincode_column_accepted_but_uninvolved(self):
        csv = b"asin,expected_price,brand,pincode\nB09W9FND7M,1499,Lapcare,110001\n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid[0].pincode, "110001")
        self.assertEqual(report.valid[0].brand, "Lapcare")

    def test_brand_whitespace_stripped(self):
        csv = b"asin,expected_price,brand\nB09W9FND7M,1499,  Lapcare  \n"
        report = parse_upload("batch.csv", csv)
        self.assertEqual(report.valid[0].brand, "Lapcare")


class ImportPathTests(unittest.TestCase):
    def test_types_shared_between_modules(self):
        from price_verifier.ingest import column_detect, input_parser
        self.assertIs(column_detect.ParseReport, input_parser.ParseReport)
        self.assertIs(column_detect.ParsedRow, input_parser.ParsedRow)
        self.assertIs(column_detect.InputValidationError, input_parser.InputValidationError)


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
