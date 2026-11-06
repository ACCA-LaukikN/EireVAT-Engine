"""
test_photo_import.py - tests for OCR parsing, validation, the Excel review loop, reverse charge and the CSV loader.
Run from this folder with:  python test_photo_import.py
OCR tests are skipped automatically if Tesseract is not installed.
"""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from openpyxl import load_workbook

from db_setup import initialise_database
from period_manager import generate_vat_period
from vat_calculator import calculate_vat3
from invoice_extractor import parse_invoice_text, parse_date, extract_invoice
from invoice_validator import validate_invoice, has_errors
from invoice_review import export_review_workbook, import_review_workbook
from import_invoice import stage_invoice, commit_invoice

HERE = os.path.dirname(os.path.abspath(__file__))
SINGLE = os.path.join(HERE, "sample_invoices", "invoice_single_rate.png")
MULTI = os.path.join(HERE, "sample_invoices", "invoice_multi_rate_photo.png")
HAVE_OCR = shutil.which("tesseract") is not None and os.path.exists(SINGLE)

SAMPLE_TEXT = """Acme Supplies Ltd
VAT No: IE1234567T
Invoice No: A-100
Date: 03/11/2026
Desk 80.00 23%
Books 20.00 0%
VAT Summary
23% 80.00 18.40
0% 20.00 0.00
Total Net: EUR 100.00
Total VAT: EUR 18.40
Total Due: EUR 118.40
"""


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class ParserTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(parse_date("12/11/2026"), "2026-11-12")      # day first
        self.assertEqual(parse_date("15 November 2026"), "2026-11-15")
        self.assertEqual(parse_date("2026-11-03"), "2026-11-03")
        self.assertIsNone(parse_date("no date here"))

    def test_parse_text(self):
        d = parse_invoice_text(SAMPLE_TEXT)
        self.assertEqual((d["supplier"], d["invoice_number"], d["invoice_date"], d["supplier_vat_no"]),
                         ("Acme Supplies Ltd", "A-100", "2026-11-03", "IE1234567T"))
        self.assertEqual([(l["vat_rate"], l["net_amount"], l["stated_vat_amount"]) for l in d["lines"]],
                         [(23.0, 80.0, 18.4), (0.0, 20.0, 0.0)])
        self.assertEqual((d["stated_net"], d["stated_vat"], d["stated_gross"]), (100.0, 18.4, 118.4))

    def test_ocr_prefix_error_is_corrected_and_noted(self):
        d = parse_invoice_text("Acme Ltd\nVAT No: 1E1234567T\nInvoice No: X1\n")
        self.assertEqual(d["supplier_vat_no"], "IE1234567T")
        self.assertTrue(d["extraction_notes"])


class ParserFormatTests(unittest.TestCase):
    def test_percent_word_unlabelled_number_and_net_line(self):
        d = parse_invoice_text("Dublin Software Solutions\nDSS-9921\n06/05/2026\nsoftware licenses EUR 850.00\n"
                               "net 850.00\nVAT 23 percent 195.50\ntotal due EUR 1045.50\n")
        self.assertEqual(d["invoice_number"], "DSS-9921")
        self.assertEqual(d["invoice_date"], "2026-05-06")
        self.assertEqual([(l["vat_rate"], l["net_amount"], l["stated_vat_amount"]) for l in d["lines"]], [(23.0, 850.0, 195.5)])
        self.assertEqual(d["stated_gross"], 1045.5)


class ReverseChargeTests(unittest.TestCase):
    def setUp(self):
        with redirect_stdout(io.StringIO()):
            self.conn, self.cur = initialise_database(":memory:", quiet=True)
            self.cur.execute("INSERT INTO businesses VALUES (1,'Demo Traders Ltd','IE1234567T','bi-monthly','invoice')")
            self.conn.commit()
            self.period = generate_vat_period(self.cur, self.conn, 1, "2026-05-01")

    def test_eu_service_invoice_self_assessed_and_total_is_net(self):
        from vat_calculator import add_invoice
        quiet(add_invoice, self.cur, self.conn, 1, self.period, "2026-05-10", "purchase", "Berlin Cloud GmbH", "BCS-1",
              "intra_eu", [{"net_amount": 1500, "vat_rate_id": 1}], is_reverse_charge=True, stated_gross=1500.00)
        boxes = calculate_vat3(self.cur, self.period)
        self.assertEqual((boxes["T1"], boxes["T2"], boxes["E2"]), (345.0, 345.0, 1500.0))
        self.assertEqual(self.cur.execute("SELECT gross_amount FROM transactions").fetchone()[0], 1500.0)

    def test_reverse_charge_invoice_that_shows_vat_is_rejected(self):
        from vat_calculator import add_invoice
        with self.assertRaises(ValueError):
            add_invoice(self.cur, self.conn, 1, self.period, "2026-05-10", "purchase", "Berlin Cloud GmbH", "BCS-2",
                        "intra_eu", [{"net_amount": 1500, "vat_rate_id": 1, "stated_vat_amount": 345.00}], is_reverse_charge=True)

    def test_validator_accepts_reverse_charge_document(self):
        d = {"supplier": "Berlin", "supplier_vat_no": "DE123456789", "invoice_number": "B1", "invoice_date": "2026-05-10",
             "is_reverse_charge": True,
             "lines": [{"vat_rate": 23.0, "vat_rate_id": 1, "net_amount": 1500.0, "stated_vat_amount": 0.0}],
             "stated_net": 1500.0, "stated_vat": 0.0, "stated_gross": 1500.0}
        self.assertFalse(has_errors(validate_invoice(d)))


class LoaderTests(unittest.TestCase):
    def test_dummy_csv_loads_and_boxes_are_right(self):
        from load_dummy_invoices import main as load
        csv_path = os.path.join(HERE, "sample_data", "dummy_invoices.csv")
        if not os.path.exists(csv_path):
            self.skipTest("sample_data/dummy_invoices.csv not present")
        with redirect_stdout(io.StringIO()):
            conn, cur = load(csv_path, ":memory:")
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 15)
        period = cur.execute("SELECT period_id FROM vat_periods WHERE period_start = '2026-05-01'").fetchone()[0]
        boxes = calculate_vat3(cur, period)
        self.assertEqual((boxes["T1"], boxes["T2"], boxes["T4"], boxes["E1"], boxes["E2"]), (1679.0, 1728.4, 49.4, 4500.0, 5500.0))


class ValidatorTests(unittest.TestCase):
    def good(self):
        return {"supplier": "Acme", "supplier_vat_no": "IE1234567T", "invoice_number": "A1", "invoice_date": "2026-10-03",
                "lines": [{"vat_rate": 23.0, "vat_rate_id": 1, "net_amount": 100.0, "stated_vat_amount": 23.0}],
                "stated_net": 100.0, "stated_vat": 23.0, "stated_gross": 123.0}

    def test_clean_invoice_passes(self):
        self.assertEqual(validate_invoice(self.good()), [])

    def test_catches_each_problem(self):
        d = self.good(); d["supplier"] = None
        self.assertTrue(has_errors(validate_invoice(d)))
        d = self.good(); d["invoice_date"] = "03/11/2026"
        self.assertTrue(has_errors(validate_invoice(d)))
        d = self.good(); d["lines"][0]["stated_vat_amount"] = 28.0          # OCR misread 23 as 28
        self.assertTrue(any("does not match" in i["message"] for i in validate_invoice(d)))
        d = self.good(); d["stated_gross"] = 130.0
        self.assertTrue(has_errors(validate_invoice(d)))
        d = self.good(); d["lines"] = []
        self.assertTrue(has_errors(validate_invoice(d)))
        d = self.good(); d["supplier_vat_no"] = "IE12345"
        self.assertTrue(any(i["level"] == "WARNING" for i in validate_invoice(d)))

    def test_negative_net_is_a_warning_not_an_error(self):
        d = self.good(); d["lines"][0].update(net_amount=-100.0, stated_vat_amount=-23.0)
        d["stated_net"], d["stated_vat"], d["stated_gross"] = -100.0, -23.0, -123.0
        issues = validate_invoice(d)
        self.assertFalse(has_errors(issues))
        self.assertTrue(any("credit note" in i["message"] for i in issues))


class ReviewWorkbookTests(unittest.TestCase):
    def test_roundtrip(self):
        d = parse_invoice_text(SAMPLE_TEXT)
        for line, rid in zip(d["lines"], (1, 5)):
            line["vat_rate_id"] = rid
        d.update(source_file="x.png", file_hash="abc", trans_type="purchase", supply_type="domestic", is_reverse_charge=False)
        path = os.path.join(tempfile.mkdtemp(), "review.xlsx")
        export_review_workbook(d, [], path)
        back = import_review_workbook(path)
        self.assertEqual(back["supplier"], "Acme Supplies Ltd")
        self.assertEqual(back["invoice_date"], "2026-11-03")
        self.assertEqual([(l["vat_rate_id"], l["net_amount"], l["stated_vat_amount"]) for l in back["lines"]],
                         [(1, 80.0, 18.4), (5, 20.0, 0.0)])
        self.assertEqual(back["file_hash"], "abc")
        self.assertEqual(back["stated_gross"], 118.4)


@unittest.skipUnless(HAVE_OCR, "Tesseract or sample images not available")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        with redirect_stdout(io.StringIO()):
            self.conn, self.cur = initialise_database(":memory:", quiet=True)
            self.cur.execute("INSERT INTO businesses VALUES (1,'Demo Traders Ltd','IE1234567T','bi-monthly','invoice')")
            self.conn.commit()
            self.period = generate_vat_period(self.cur, self.conn, 1, "2026-11-01")
        self.tmp = tempfile.mkdtemp()

    def test_single_rate_photo_to_database(self):
        review = os.path.join(self.tmp, "r1.xlsx")
        _, data, issues = quiet(stage_invoice, self.cur, SINGLE, 1, self.period, review)
        self.assertFalse(has_errors(issues))
        invoice_id = quiet(commit_invoice, self.cur, self.conn, 1, self.period, review)
        self.assertIsNotNone(invoice_id)
        boxes = calculate_vat3(self.cur, self.period)
        self.assertEqual((boxes["T2"], boxes["T4"]), (23.0, 23.0))        # purchase VAT reclaimable

    def test_multi_rate_photo_creates_two_rows_and_flags_vat_number_slip(self):
        review = os.path.join(self.tmp, "r2.xlsx")
        _, data, _ = quiet(stage_invoice, self.cur, MULTI, 1, self.period, review)
        self.assertTrue(any("1E" in n for n in data["extraction_notes"]))
        self.assertIsNotNone(quiet(commit_invoice, self.cur, self.conn, 1, self.period, review))
        self.assertEqual(self.cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)

    def test_a_bad_correction_is_blocked_then_fixed(self):
        review = os.path.join(self.tmp, "r3.xlsx")
        quiet(stage_invoice, self.cur, SINGLE, 1, self.period, review)
        wb = load_workbook(review); wb["Review"]["D14"] = 28.00; wb.save(review)      # simulate a wrong edit
        self.assertIsNone(quiet(commit_invoice, self.cur, self.conn, 1, self.period, review))
        self.assertEqual(self.cur.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)
        wb = load_workbook(review); wb["Review"]["D14"] = 23.00; wb.save(review)      # fix it
        self.assertIsNotNone(quiet(commit_invoice, self.cur, self.conn, 1, self.period, review))

    def test_same_photo_cannot_be_imported_twice(self):
        review = os.path.join(self.tmp, "r4.xlsx")
        quiet(stage_invoice, self.cur, SINGLE, 1, self.period, review)
        quiet(commit_invoice, self.cur, self.conn, 1, self.period, review)
        review2 = os.path.join(self.tmp, "r5.xlsx")
        _, _, issues = quiet(stage_invoice, self.cur, SINGLE, 1, self.period, review2)
        self.assertTrue(has_errors(issues))
        self.assertIsNone(quiet(commit_invoice, self.cur, self.conn, 1, self.period, review2))


if __name__ == "__main__":
    unittest.main()
