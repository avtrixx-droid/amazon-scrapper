"""Offline tests for automatic column detection on vendor uploads. Run:
  python -m unittest price_verifier.tests.test_column_detect
"""

from __future__ import annotations

import io
import time
import unittest

from openpyxl import Workbook

from price_verifier.ingest.column_detect import (
    FIELDS,
    DetectionResult,
    RawTable,
    detect_columns,
    extract_asin,
    load_table,
    parse_price,
    parse_rows,
)
from price_verifier.ingest.input_parser import InputValidationError, parse_upload


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def detect_csv(text: str, encoding: str = "utf-8", filename: str = "f.csv") -> DetectionResult:
    return detect_columns(load_table(filename, text.encode(encoding)))


def mapped_header(det: DetectionResult, field: str):
    idx = det.mapping[field]
    return None if idx is None else det.table.headers[idx]


class ExtractAsinTests(unittest.TestCase):
    def test_plain_and_messy_values(self):
        self.assertEqual(extract_asin("B09W9FND7M"), "B09W9FND7M")
        self.assertEqual(extract_asin("  b09w9fnd7m "), "B09W9FND7M")
        self.assertEqual(extract_asin('"B09W9FND7M",'), "B09W9FND7M")
        self.assertEqual(extract_asin("'B09W9FND7M"), "B09W9FND7M")
        self.assertEqual(extract_asin("B09W9FND7M."), "B09W9FND7M")

    def test_urls(self):
        cases = [
            "https://www.amazon.in/dp/B09W9FND7M",
            "https://www.amazon.in/Lapcare-Webcam-720p/dp/B09W9FND7M/ref=sr_1_1?keywords=x",
            "amazon.in/gp/product/B09W9FND7M",
            "https://www.amazon.com/product/B09W9FND7M?th=1",
            "https://www.amazon.co.uk/something?tag=x&asin=B09W9FND7M",
            "https://www.amazon.in/dp/b09w9fnd7m?psc=1",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(extract_asin(url), "B09W9FND7M")

    def test_isbn_style(self):
        self.assertEqual(extract_asin("812345678X"), "812345678X")
        self.assertEqual(extract_asin("0123456789"), "0123456789")
        # Excel stored it as a number and dropped the leading zero.
        self.assertEqual(extract_asin(123456789), "0123456789")
        self.assertEqual(extract_asin(8123456789.0), "8123456789")
        self.assertEqual(extract_asin("123456789.0"), "0123456789")

    def test_rejects(self):
        for v in (None, "", "B09W9F", "X09W9FND7M", "hello world", 1499, 1499.5, True,
                  "https://www.amazon.in/s?k=webcam", "https://amzn.to/3abcd", "B09W9FND7M1"):
            with self.subTest(v=v):
                self.assertIsNone(extract_asin(v))


class ParsePriceTests(unittest.TestCase):
    def test_formats(self):
        cases = {
            "1499": 1499.0, "1499.5": 1499.5, "₹1,499.00": 1499.0, "Rs. 1499": 1499.0,
            "Rs 1,499/-": 1499.0, "INR 1499": 1499.0, "1,49,999": 149999.0, "1,234,567": 1234567.0,
            "₹ 1,499": 1499.0, "Rs.1499": 1499.0, "1499 INR": 1499.0, "1499/-": 1499.0,
            "₹1 499": 1499.0, 1499: 1499.0, 999.5: 999.5,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_price(raw), expected)

    def test_rejects(self):
        for v in (None, "", "N/A", "abc", "0", 0, -5, "-100", "10%", "1,2", "1.499,00", True):
            with self.subTest(v=v):
                self.assertIsNone(parse_price(v))


class LoadTableTests(unittest.TestCase):
    def test_semicolon_csv(self):
        det = detect_csv("ASIN;Selling Price;Brand\nB09W9FND7M;1.499;Lapcare\nB08N5WRWNW;999;Lapcare\n")
        self.assertEqual(det.table.headers, ["ASIN", "Selling Price", "Brand"])
        self.assertEqual(det.mapping, {"asin": 0, "expected_price": 1, "brand": 2})

    def test_semicolon_csv_with_unquoted_commas(self):
        table = load_table("f.csv", "asin;price\nB09W9FND7M;1,499\nB08N5WRWNW;2,999\n".encode())
        self.assertEqual(table.headers, ["asin", "price"])
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual([r.expected_price for r in report.valid], [1499.0, 2999.0])

    def test_tab_csv(self):
        det = detect_csv("ASIN\tSP\tBrand\nB09W9FND7M\t1499\tLapcare\n")
        self.assertEqual(det.mapping, {"asin": 0, "expected_price": 1, "brand": 2})

    def test_pipe_csv(self):
        det = detect_csv("ASIN|SP\nB09W9FND7M|1499\nB08N5WRWNW|999\n")
        self.assertEqual(det.mapping["asin"], 0)
        self.assertEqual(det.mapping["expected_price"], 1)

    def test_utf8_bom(self):
        data = "﻿ASIN,SP\nB09W9FND7M,₹1499\n".encode("utf-8")
        table = load_table("f.csv", data)
        self.assertEqual(table.headers[0], "ASIN")
        report = parse_rows(table, detect_columns(table).mapping)
        self.assertEqual(report.valid[0].expected_price, 1499.0)

    def test_cp1252_file_with_rs_prices(self):
        # Windows Excel "CSV" export: cp1252, curly quotes / en dash in text.
        text = "Brand Name,ASIN No.,Selling Price,Notes\nLapcare,B09W9FND7M,Rs. 1499,Best–seller\n"
        data = text.encode("cp1252")
        with self.assertRaises(UnicodeDecodeError):
            data.decode("utf-8")
        det = detect_columns(load_table("f.csv", data))
        self.assertEqual(mapped_header(det, "asin"), "ASIN No.")
        self.assertEqual(mapped_header(det, "expected_price"), "Selling Price")
        self.assertEqual(mapped_header(det, "brand"), "Brand Name")
        report = parse_rows(det.table, det.mapping)
        self.assertEqual(report.valid[0].expected_price, 1499.0)

    def test_utf16_unicode_text_export(self):
        data = "ASIN\tSP\r\nB09W9FND7M\t1499\r\n".encode("utf-16")
        det = detect_columns(load_table("f.csv", data))
        self.assertEqual(det.mapping["asin"], 0)
        self.assertEqual(det.mapping["expected_price"], 1)

    def test_xls_rejected_with_save_as_hint(self):
        with self.assertRaises(InputValidationError) as ctx:
            load_table("old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
        self.assertIn("Save As", str(ctx.exception))

    def test_xls_renamed_to_xlsx_rejected(self):
        with self.assertRaises(InputValidationError) as ctx:
            load_table("renamed.xlsx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
        self.assertIn("Save As", str(ctx.exception))

    def test_unsupported_extension(self):
        with self.assertRaises(InputValidationError) as ctx:
            load_table("list.pdf", b"%PDF")
        self.assertIn(".pdf", str(ctx.exception))

    def test_corrupt_xlsx(self):
        with self.assertRaises(InputValidationError):
            load_table("broken.xlsx", b"not really a zip file")

    def test_empty_inputs(self):
        for name, data in (("e.csv", b""), ("e.csv", b"\n\n , ,\n"), ("e.xlsx", make_xlsx({"S": []}))):
            with self.subTest(name=name, data=data[:10]):
                with self.assertRaises(InputValidationError):
                    load_table(name, data)

    def test_blank_rows_in_middle_removed_but_row_numbers_kept(self):
        table = load_table("f.csv", b"ASIN,SP\nB09W9FND7M,1499\n,\n\nB08N5WRWNW,999\n")
        self.assertEqual(len(table.rows), 2)
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual([r.row_number for r in report.rows], [2, 5])

    def test_trailing_empty_columns_trimmed(self):
        table = load_table("f.csv", b"ASIN,SP,,\nB09W9FND7M,1499,,\n")
        self.assertEqual(table.headers, ["ASIN", "SP"])


class HeaderRowTests(unittest.TestCase):
    def test_title_rows_above_header_csv(self):
        text = (
            "Price list – Sept 2026,,\n"
            "Lapcare India Pvt Ltd,,\n"
            ",,\n"
            "ASIN No.,SP,Brand Name\n"
            "B09W9FND7M,1499,Lapcare\n"
            "B08N5WRWNW,999,Lapcare\n"
        )
        det = detect_csv(text)
        self.assertEqual(det.table.header_row, 4)
        self.assertEqual(det.table.headers, ["ASIN No.", "SP", "Brand Name"])
        self.assertEqual(det.mapping, {"asin": 0, "expected_price": 1, "brand": 2})
        self.assertTrue(any("Skipped 2 lines" in w and "Price list" in w for w in det.warnings))
        report = parse_rows(det.table, det.mapping)
        self.assertEqual([r.row_number for r in report.rows], [5, 6])

    def test_title_rows_xlsx_row_numbers(self):
        data = make_xlsx({"Sheet1": [
            ["Price list – Sept 2026"],
            [],
            ["Sr No", "ASIN", "Product", "MRP", "SP", "Brand"],
            [1, "B09W9FND7M", "Webcam", 2499, 1499, "Lapcare"],
            [],
            [2, "B08N5WRWNW", "USB Hub", 1299, 999, "Lapcare"],
            [3, "bad-asin", "Mouse", 599, 399, "Lapcare"],
        ]})
        report = parse_upload("list.xlsx", data)
        self.assertEqual([r.row_number for r in report.rows], [4, 6, 7])
        self.assertEqual([r.expected_price for r in report.valid], [1499.0, 999.0])
        self.assertIn("10 characters", report.invalid[0].reason)
        self.assertEqual(report.invalid[0].row_number, 7)

    def test_banner_with_two_cells_not_mistaken_for_header(self):
        text = (
            "Vendor: Lapcare,Date: 01-09-2026,,\n"
            "ASIN,Item Name,MRP,Selling Price\n"
            "B09W9FND7M,Webcam,2499,1499\n"
            "B08N5WRWNW,USB Hub,1299,999\n"
        )
        det = detect_csv(text)
        self.assertEqual(det.table.header_row, 2)
        self.assertEqual(mapped_header(det, "expected_price"), "Selling Price")

    def test_no_header_row(self):
        rows = "".join(f"B0{i:08d},{1000 + i},Lapcare\n" for i in range(5))
        det = detect_csv(rows)
        self.assertEqual(det.table.header_row, 0)
        self.assertEqual(det.table.headers, ["Column A", "Column B", "Column C"])
        self.assertEqual(det.mapping["asin"], 0)
        self.assertEqual(det.mapping["expected_price"], 1)
        self.assertTrue(any("headings" in w for w in det.warnings))
        report = parse_rows(det.table, det.mapping)
        self.assertEqual(report.rows[0].row_number, 1)
        self.assertEqual(report.valid_rows, 5)

    def test_no_header_first_row_has_bad_asin(self):
        rows = "B09W9F,Webcam,Lapcare,1499\n" + "".join(
            f"B0{i:08d},Item {i},Lapcare,{1000 + i}\n" for i in range(4))
        det = detect_csv(rows)
        self.assertEqual(det.table.header_row, 0)
        report = parse_rows(det.table, det.mapping)
        self.assertEqual(report.total_rows, 5)
        self.assertEqual(report.invalid[0].row_number, 1)
        self.assertIn("10 characters", report.invalid[0].reason)

    def test_no_header_brand_inferred_from_content(self):
        brands = ["Lapcare", "Portronics", "boAt"]
        rows = "".join(f"B0{i:08d},{1000 + i},{brands[i % 3]}\n" for i in range(12))
        det = detect_csv(rows)
        self.assertEqual(det.mapping["brand"], 2)
        self.assertTrue(any("holds the brand" in w for w in det.warnings))

    def test_no_header_with_title_line(self):
        det = detect_csv("Price list Sept 2026\nB09W9FND7M,1499\nB08N5WRWNW,999\n")
        self.assertEqual(det.table.header_row, 0)
        self.assertEqual(len(det.table.rows), 2)
        self.assertEqual(det.table.row_numbers, [2, 3])

    def test_blank_header_cells_synthesized(self):
        det = detect_csv("ASIN,,Brand\nB09W9FND7M,1499,Lapcare\n")
        self.assertEqual(det.table.headers, ["ASIN", "Column B", "Brand"])
        self.assertEqual(det.mapping["expected_price"], 1)

    def test_blank_header_over_asin_column(self):
        det = detect_csv(",Selling Price,Brand\nB09W9FND7M,1499,Lapcare\nB08N5WRWNW,999,Lapcare\n")
        self.assertEqual(det.table.header_row, 1)
        self.assertEqual(det.table.headers[0], "Column A")
        self.assertEqual(det.mapping, {"asin": 0, "expected_price": 1, "brand": 2})


class ColumnMappingTests(unittest.TestCase):
    def test_vendor_style_headers(self):
        det = detect_csv("Brand Name,ASIN No.,SP\nLapcare,B09W9FND7M,1499\nLapcare,B08N5WRWNW,999\n")
        self.assertEqual(det.mapping, {"asin": 1, "expected_price": 2, "brand": 0})
        self.assertGreaterEqual(det.confidence["asin"], 0.9)
        self.assertGreaterEqual(det.confidence["expected_price"], 0.9)
        self.assertGreaterEqual(det.confidence["brand"], 0.8)
        self.assertEqual(det.warnings, [])

    def test_dotted_abbreviations(self):
        det = detect_csv("A.S.I.N,S.P.,M.R.P.\nB09W9FND7M,1499,2499\n")
        self.assertEqual(mapped_header(det, "expected_price"), "S.P.")

    def test_url_column_instead_of_asin(self):
        det = detect_csv(
            "Amazon Link,Selling Price\n"
            "https://www.amazon.in/Lapcare-Webcam/dp/B09W9FND7M/ref=sr_1_1,1499\n"
            "https://www.amazon.in/dp/B08N5WRWNW?th=1,999\n"
        )
        self.assertEqual(det.mapping["asin"], 0)
        report = parse_rows(det.table, det.mapping)
        self.assertEqual([r.asin for r in report.valid], ["B09W9FND7M", "B08N5WRWNW"])

    def test_asin_found_by_content_under_unhelpful_header(self):
        det = detect_csv("Code,Selling Price\nB09W9FND7M,1499\nB08N5WRWNW,999\n")
        self.assertEqual(det.mapping["asin"], 0)

    def test_mrp_and_sp_picks_sp(self):
        for header in ("SP", "Selling Price", "Offer Price", "Price"):
            with self.subTest(header=header):
                det = detect_csv(
                    f"ASIN,MRP,{header},Qty\nB09W9FND7M,2499,1499,5\nB08N5WRWNW,1299,999,10\n"
                )
                self.assertEqual(mapped_header(det, "expected_price"), header)
                mrp_rank = [o.header for o in det.options["expected_price"]].index("MRP")
                self.assertGreater(mrp_rank, 0)

    def test_mrp_before_sp_in_column_order(self):
        det = detect_csv("ASIN,SP,MRP\nB09W9FND7M,1499,2499\n")
        self.assertEqual(mapped_header(det, "expected_price"), "SP")

    def test_only_mrp_picked_with_warning(self):
        det = detect_csv("ASIN,Product,MRP\nB09W9FND7M,Webcam,2499\nB08N5WRWNW,Hub,1299\n")
        self.assertEqual(mapped_header(det, "expected_price"), "MRP")
        self.assertTrue(any("MRP" in w and "selling-price" in w for w in det.warnings))
        self.assertLess(det.confidence["expected_price"], 0.5)

    def test_unhelpful_numeric_header_warns(self):
        det = detect_csv("ASIN,Col2\nB09W9FND7M,1499\nB08N5WRWNW,999\n")
        self.assertEqual(det.mapping["expected_price"], 1)
        self.assertTrue(any("'Col2'" in w for w in det.warnings))

    def test_serial_number_and_pincode_not_chosen_as_price(self):
        det = detect_csv(
            "Sr,ASIN,Pincode,Rate\n"
            "1,B09W9FND7M,110001,1499\n2,B08N5WRWNW,560001,999\n3,B07XYZ1234,400001,599\n"
        )
        self.assertEqual(mapped_header(det, "expected_price"), "Rate")

    def test_serial_column_loses_even_without_price_header(self):
        det = detect_csv("S.No,ASIN,Amount\n1,B09W9FND7M,1499\n2,B08N5WRWNW,999\n3,B07XYZ1234,599\n")
        self.assertEqual(mapped_header(det, "expected_price"), "Amount")

    def test_qty_not_chosen_over_price(self):
        det = detect_csv("ASIN,Qty,Price\nB09W9FND7M,5,1499\nB08N5WRWNW,10,999\n")
        self.assertEqual(mapped_header(det, "expected_price"), "Price")

    def test_percent_column_not_price(self):
        det = detect_csv("ASIN,Discount %,Expected Price\nB09W9FND7M,20,1499\n")
        self.assertEqual(mapped_header(det, "expected_price"), "Expected Price")

    def test_ambiguous_strong_price_headers_warn(self):
        det = detect_csv("ASIN,Selling Price,Offer Price\nB09W9FND7M,1499,1399\n")
        self.assertEqual(mapped_header(det, "expected_price"), "Selling Price")
        self.assertTrue(any("Please confirm" in w for w in det.warnings))

    def test_asin_column_never_picked_as_price(self):
        # ISBN-style numeric ASINs are numbers, but they're the ASIN.
        data = make_xlsx({"S": [["ASIN", "Price"], [8123456789, 499], [9876543210, 299]]})
        det = detect_columns(load_table("f.xlsx", data))
        self.assertEqual(det.mapping["asin"], 0)
        self.assertEqual(det.mapping["expected_price"], 1)
        report = parse_rows(det.table, det.mapping)
        self.assertEqual([r.asin for r in report.valid], ["8123456789", "9876543210"])

    def test_isbn_string_asins(self):
        det = detect_csv("ASIN,SP\n812345678X,499\n0123456789,299\n")
        report = parse_rows(det.table, det.mapping)
        self.assertEqual([r.asin for r in report.valid], ["812345678X", "0123456789"])

    def test_brand_absent(self):
        det = detect_csv("ASIN,Item Name,SP\nB09W9FND7M,Lapcare Webcam 720p HD with Mic,1499\n")
        self.assertIsNone(det.mapping["brand"])
        self.assertTrue(any("No brand column" in w for w in det.warnings))
        report = parse_rows(det.table, det.mapping)
        self.assertEqual(report.valid_rows, 1)
        self.assertIsNone(report.valid[0].brand)

    def test_brand_synonyms(self):
        for header in ("Brand", "Brand Name", "Make", "Manufacturer", "Company"):
            with self.subTest(header=header):
                det = detect_csv(f"ASIN,{header},SP\nB09W9FND7M,Lapcare,1499\n")
                self.assertEqual(det.mapping["brand"], 1)

    def test_product_title_not_taken_as_brand_with_headers(self):
        det = detect_csv("ASIN,Description,SP\nB09W9FND7M,Webcam,1499\n")
        self.assertIsNone(det.mapping["brand"])

    def test_options_rank_all_columns(self):
        det = detect_csv("Brand,ASIN,MRP,SP\nLapcare,B09W9FND7M,2499,1499\n")
        for f in FIELDS:
            with self.subTest(field=f):
                opts = det.options[f]
                self.assertEqual(sorted(o.index for o in opts), [0, 1, 2, 3])
                self.assertEqual(opts[0].index, det.mapping[f])
                scores = [o.score for o in opts]
                self.assertEqual(scores, sorted(scores, reverse=True))
                self.assertTrue(all(o.reason for o in opts))
        asin_opt = det.options["asin"][0]
        self.assertEqual(asin_opt.samples, ["B09W9FND7M"])

    def test_no_column_reused_across_fields(self):
        det = detect_csv("ASIN,Brand\nB09W9FND7M,Lapcare\n")
        self.assertEqual(det.mapping["asin"], 0)
        self.assertIsNone(det.mapping["expected_price"])
        used = [v for v in det.mapping.values() if v is not None]
        self.assertEqual(len(used), len(set(used)))

    def test_preview(self):
        rows = "".join(f"B0{i:08d},{1000 + i}.5\n" for i in range(8))
        det = detect_csv("ASIN,SP\n" + rows)
        self.assertEqual(len(det.preview), 5)
        self.assertEqual(det.preview[0], ["B000000000", "1000.5"])

    def test_duplicate_warning(self):
        det = detect_csv("ASIN,SP\nB09W9FND7M,1499\nB09W9FND7M,1499\nB08N5WRWNW,999\n")
        self.assertTrue(any("more than once" in w for w in det.warnings))


class SheetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.data = make_xlsx({
            "Instructions": [["Fill in the Data sheet"], ["Prices are in rupees"]],
            "Data": [["ASIN", "SP", "Brand"], ["B09W9FND7M", 1499, "Lapcare"], ["B08N5WRWNW", 999, "Lapcare"]],
            "Old": [["ASIN", "SP"], ["B07XYZ1234", 599]],
        })

    def test_auto_picks_data_sheet(self):
        table = load_table("f.xlsx", self.data)
        self.assertEqual(table.sheet_names, ["Instructions", "Data", "Old"])
        self.assertEqual(table.sheet_name, "Data")
        det = detect_columns(table)
        self.assertTrue(any("3 sheets" in w and "'Data'" in w for w in det.warnings))

    def test_explicit_sheet_name(self):
        table = load_table("f.xlsx", self.data, sheet_name="Old")
        self.assertEqual(table.sheet_name, "Old")
        report = parse_rows(table, detect_columns(table).mapping)
        self.assertEqual([r.asin for r in report.valid], ["B07XYZ1234"])

    def test_explicit_sheet_name_case_insensitive(self):
        self.assertEqual(load_table("f.xlsx", self.data, sheet_name=" old ").sheet_name, "Old")

    def test_unknown_sheet_name(self):
        with self.assertRaises(InputValidationError) as ctx:
            load_table("f.xlsx", self.data, sheet_name="Nope")
        self.assertIn("Instructions, Data, Old", str(ctx.exception))

    def test_falls_back_to_first_nonempty_sheet(self):
        data = make_xlsx({"Blank": [], "Stuff": [["a", "b"], ["c", "d"]]})
        self.assertEqual(load_table("f.xlsx", data).sheet_name, "Stuff")

    def test_csv_has_no_sheets(self):
        table = load_table("f.csv", b"ASIN,SP\nB09W9FND7M,1499\n")
        self.assertEqual(table.sheet_names, [])
        self.assertIsNone(table.sheet_name)


class ParseRowsTests(unittest.TestCase):
    def _table(self, text: str) -> RawTable:
        return load_table("f.csv", text.encode())

    def test_indian_grouping_and_currency(self):
        table = self._table('ASIN,SP\nB09W9FND7M,"1,49,999"\nB08N5WRWNW,"Rs 1,499/-"\nB07XYZ1234,INR 99\n')
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual([r.expected_price for r in report.valid], [149999.0, 1499.0, 99.0])

    def test_invalid_price_reasons(self):
        table = self._table("ASIN,SP\nB000000001,\nB000000002,0\nB000000003,-10\nB000000004,call me\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        reasons = [r.reason for r in report.invalid]
        self.assertIn("blank", reasons[0])
        self.assertIn("more than 0", reasons[1])
        self.assertIn("more than 0", reasons[2])
        self.assertIn("not a number", reasons[3])

    def test_invalid_asin_reasons(self):
        table = self._table("ASIN,SP\n,100\nB09W9F,100\nhttps://www.amazon.in/s?k=x,100\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        reasons = [r.reason for r in report.invalid]
        self.assertIn("blank", reasons[0])
        self.assertIn("10 characters", reasons[1])
        self.assertIn("link", reasons[2])

    def test_duplicates_reference_first_row(self):
        table = self._table("Title\nASIN,SP\nB09W9FND7M,1499\nB08N5WRWNW,999\nb09w9fnd7m,1599\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual(report.valid_rows, 2)
        dup = report.invalid[0]
        self.assertEqual(dup.row_number, 5)
        self.assertEqual(dup.reason, "Duplicate of row 3 (same ASIN listed twice)")

    def test_duplicate_after_invalid_first_occurrence_is_valid(self):
        table = self._table("ASIN,SP\nB09W9FND7M,oops\nB09W9FND7M,1499\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual(report.valid_rows, 1)
        self.assertEqual(report.valid[0].row_number, 3)

    def test_brand_handling(self):
        table = self._table("ASIN,SP,Brand\nB09W9FND7M,1499,  Lapcare \nB08N5WRWNW,999,\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1, "brand": 2})
        self.assertEqual([r.brand for r in report.valid], ["Lapcare", None])
        report = parse_rows(table, {"asin": 0, "expected_price": 1, "brand": None})
        self.assertEqual([r.brand for r in report.valid], [None, None])

    def test_vendor_override_mapping(self):
        # Vendor overrides the detected price column with MRP on the confirm screen.
        table = self._table("ASIN,MRP,SP\nB09W9FND7M,2499,1499\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual(report.valid[0].expected_price, 2499.0)

    def test_pincode_by_header(self):
        table = self._table("ASIN,SP,Pin Code\nB09W9FND7M,1499,110001\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual(report.valid[0].pincode, "110001")

    def test_missing_required_mapping_raises(self):
        table = self._table("ASIN,SP\nB09W9FND7M,1499\n")
        for mapping in ({"asin": 0}, {"expected_price": 1}, {"asin": 0, "expected_price": None},
                        {"asin": 0, "expected_price": 0}, {"asin": 0, "expected_price": 7}):
            with self.subTest(mapping=mapping):
                with self.assertRaises(InputValidationError):
                    parse_rows(table, mapping)

    def test_report_shape(self):
        table = self._table("ASIN,SP\nB09W9FND7M,1499\nbad,1\n")
        report = parse_rows(table, {"asin": 0, "expected_price": 1})
        self.assertEqual((report.total_rows, report.valid_rows, report.invalid_rows), (2, 1, 1))
        self.assertEqual(report.columns_found, ["ASIN", "SP"])


class ParseUploadWrapperTests(unittest.TestCase):
    def test_real_world_sheet(self):
        data = make_xlsx({
            "Read me": [["This file lists our agreed prices"]],
            "Sept 2026": [
                ["Lapcare – Amazon price list, Sept 2026"],
                [],
                ["S.No", "Product Name", "Amazon Link", "Brand Name", "MRP", "Selling Price", "Remarks"],
                [1, "Webcam 720p", "https://www.amazon.in/dp/B09W9FND7M", "Lapcare", 2499, "₹1,499", ""],
                [2, "USB Hub", "https://www.amazon.in/gp/product/B08N5WRWNW", "Lapcare", 1299, 999, "new"],
                [3, "Mouse", "https://www.amazon.in/dp/B09W9FND7M", "Lapcare", 599, 399, ""],
            ],
        })
        report = parse_upload("prices.xlsx", data)
        self.assertEqual(report.valid_rows, 2)
        self.assertEqual([r.expected_price for r in report.valid], [1499.0, 999.0])
        self.assertEqual({r.brand for r in report.valid}, {"Lapcare"})
        self.assertEqual(report.invalid[0].row_number, 6)
        self.assertIn("Duplicate of row 4", report.invalid[0].reason)
        self.assertTrue(report.warnings)

    def test_friendly_error_lists_columns(self):
        with self.assertRaises(InputValidationError) as ctx:
            parse_upload("f.csv", b"Name,Qty\nWebcam,5\n")
        msg = str(ctx.exception)
        self.assertIn("ASIN", msg)
        self.assertIn("'Name'", msg)
        self.assertNotIn("Traceback", msg)


class PerformanceTests(unittest.TestCase):
    N = 10_000

    def _rows(self):
        return [[f"B{i:09d}", f"Product {i}", 2000 + i % 500, 1000 + i % 700, ["Lapcare", "boAt"][i % 2]]
                for i in range(self.N)]

    def test_10k_rows_csv_under_one_second(self):
        lines = ["S.No,ASIN,Item,MRP,SP,Brand"]
        lines += [f"{i + 1},{r[0]},{r[1]},{r[2]},{r[3]},{r[4]}" for i, r in enumerate(self._rows())]
        data = ("\n".join(lines) + "\n").encode()
        start = time.perf_counter()
        report = parse_upload("big.csv", data)
        elapsed = time.perf_counter() - start
        self.assertEqual(report.valid_rows, self.N)
        self.assertLess(elapsed, 1.0, f"took {elapsed:.2f}s")

    def test_10k_rows_xlsx(self):
        data = make_xlsx({"S": [["ASIN", "Item", "MRP", "SP", "Brand"]] + self._rows()})
        start = time.perf_counter()
        table = load_table("big.xlsx", data)
        load_elapsed = time.perf_counter() - start
        start = time.perf_counter()
        report = parse_rows(table, detect_columns(table).mapping)
        detect_parse_elapsed = time.perf_counter() - start
        self.assertEqual(report.valid_rows, self.N)
        self.assertEqual(report.valid[0].expected_price, 1000.0)
        # openpyxl's XML parsing dominates; our own detect+parse must stay well under 1s.
        self.assertLess(detect_parse_elapsed, 1.0, f"detect+parse took {detect_parse_elapsed:.2f}s")
        self.assertLess(load_elapsed, 5.0, f"xlsx load took {load_elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
