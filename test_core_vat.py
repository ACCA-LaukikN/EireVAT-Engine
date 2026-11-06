"""
test_core_vat.py - tests for the core VAT logic (transactions, invoices, periods, VAT3 boxes, schema upgrade).
Run from this folder with:  python test_core_vat.py
Uses an in-memory database, so your real vat3.db is never touched.
"""
import io
import sqlite3
import unittest
from contextlib import redirect_stdout

from db_setup import initialise_database, migrate_schema, create_tables
from vat_calculator import add_transaction, add_invoice, calculate_vat3, generate_vat3_report
from period_manager import generate_vat_period, mark_period_filed, check_overdue_periods

STANDARD, ZERO = 1, 5


class VatToolTests(unittest.TestCase):

    def setUp(self):
        with redirect_stdout(io.StringIO()):
            self.conn, self.cur = initialise_database(":memory:", quiet=True)
            self.cur.execute("INSERT INTO businesses (business_id, business_name, vat_registration_no, "
                             "vat_period_type, accounting_basis) VALUES (1, 'Test Ltd', 'IE0000000T', 'bi-monthly', 'invoice')")
            self.cur.execute("INSERT INTO businesses (business_id, business_name, vat_registration_no, "
                             "vat_period_type, accounting_basis) VALUES (2, 'Other Ltd', 'IE1111111T', 'bi-monthly', 'invoice')")
            self.conn.commit()
            self.period = generate_vat_period(self.cur, self.conn, 1, "2026-05-01")  # 1 May - 30 Jun

    def run_quiet(self, fn, *args, **kwargs):
        with redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def count(self, table):
        return self.cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # --- duplicates -------------------------------------------------
    def test_same_invoice_number_different_suppliers_both_accepted(self):
        a = self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-10", "purchase",
                           "Supplier A", 100, STANDARD, "INV-001", "domestic")
        b = self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-11", "purchase",
                           "Supplier B", 200, STANDARD, "INV-001", "domestic")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)

    def test_same_supplier_same_invoice_same_rate_is_skipped(self):
        args = (self.cur, self.conn, 1, self.period, "2026-05-10", "purchase", "Supplier A", 100, STANDARD, "INV-001", "domestic")
        self.assertIsNotNone(self.run_quiet(add_transaction, *args))
        self.assertIsNone(self.run_quiet(add_transaction, *args))
        self.assertEqual(self.count("transactions"), 1)

    def test_supplier_name_match_ignores_case_and_spaces(self):
        self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-10", "purchase",
                       "Supplier A", 100, STANDARD, "INV-001", "domestic")
        again = self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-10", "purchase",
                               "  supplier a ", 100, STANDARD, "INV-001", "domestic")
        self.assertIsNone(again)

    # --- reports and filing ----------------------------------------
    def test_report_twice_keeps_one_return_row(self):
        self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-10", "sale",
                       "Customer", 1000, STANDARD, "S-1", "domestic")
        self.run_quiet(generate_vat3_report, self.cur, self.conn, self.period, 1)
        self.run_quiet(generate_vat3_report, self.cur, self.conn, self.period, 1)
        self.assertEqual(self.count("vat3_returns"), 1)

    def test_filing_flow(self):
        self.cur.execute("UPDATE vat_periods SET due_date = '2020-01-01' WHERE period_id = ?", (self.period,))
        self.conn.commit()
        self.assertEqual(len(self.run_quiet(check_overdue_periods, self.cur, 1)), 1)  # overdue while open

        with self.assertRaises(ValueError):                     # cannot file before generating the return
            mark_period_filed(self.cur, self.conn, self.period)
        self.run_quiet(generate_vat3_report, self.cur, self.conn, self.period, 1)
        self.run_quiet(mark_period_filed, self.cur, self.conn, self.period)

        self.assertEqual(len(self.run_quiet(check_overdue_periods, self.cur, 1)), 0)  # no longer overdue
        with self.assertRaises(ValueError):                     # filed periods are locked
            add_transaction(self.cur, self.conn, 1, self.period, "2026-05-10", "sale", "C", 10, STANDARD, "S-9", "domestic")

    # --- validation --------------------------------------------------
    def test_validation_rules(self):
        base = dict(cursor=self.cur, conn=self.conn, business_id=1, period_id=self.period, date="2026-05-10",
                    trans_type="sale", counterparty="C", net_amount=100, vat_rate_id=STANDARD,
                    invoice_number="X-1", supply_type="domestic")
        def attempt(**changes):
            kw = dict(base, **changes)
            with redirect_stdout(io.StringIO()):
                return add_transaction(kw.pop("cursor"), kw.pop("conn"), kw.pop("business_id"), kw.pop("period_id"),
                                       kw.pop("date"), kw.pop("trans_type"), kw.pop("counterparty"), kw.pop("net_amount"),
                                       kw.pop("vat_rate_id"), kw.pop("invoice_number"), kw.pop("supply_type"))
        with self.assertRaises(ValueError): attempt(date="2026-07-01")        # outside period
        with self.assertRaises(ValueError): attempt(date="10/05/2026")        # wrong format
        with self.assertRaises(ValueError): attempt(business_id=2)            # period belongs to another business
        with self.assertRaises(ValueError): attempt(trans_type="refund")
        with self.assertRaises(ValueError): attempt(vat_rate_id=99)
        with self.assertRaises(ValueError): attempt(invoice_number="  ")

    def test_period_must_start_on_first_of_month(self):
        with self.assertRaises(ValueError):
            generate_vat_period(self.cur, self.conn, 1, "2026-05-15")

    # --- multi-rate invoices ---------------------------------------
    def test_multi_rate_invoice_creates_one_header_and_one_row_per_rate(self):
        invoice_id = self.run_quiet(add_invoice, self.cur, self.conn, 1, self.period, "2026-05-12", "purchase",
                                    "Stationery Ltd", "INV-77", "domestic",
                                    [{"net_amount": 100, "vat_rate_id": STANDARD, "stated_vat_amount": 23.00},
                                     {"net_amount": 50, "vat_rate_id": ZERO}],
                                    stated_gross=173.00)
        self.assertIsNotNone(invoice_id)
        self.assertEqual(self.count("invoices"), 1)
        self.assertEqual(self.count("transactions"), 2)
        self.assertEqual(calculate_vat3(self.cur, self.period)["T2"], 23.00)

    def test_lines_with_same_rate_are_combined(self):
        self.run_quiet(add_invoice, self.cur, self.conn, 1, self.period, "2026-05-12", "purchase", "S", "INV-5", "domestic",
                       [{"net_amount": 60, "vat_rate_id": STANDARD}, {"net_amount": 40, "vat_rate_id": STANDARD}])
        self.assertEqual(self.count("transactions"), 1)
        self.assertEqual(self.cur.execute("SELECT net_amount FROM transactions").fetchone()[0], 100.0)

    def test_invoice_imported_twice_is_skipped(self):
        args = (self.cur, self.conn, 1, self.period, "2026-05-12", "purchase", "S", "INV-5", "domestic",
                [{"net_amount": 100, "vat_rate_id": STANDARD}])
        self.assertIsNotNone(self.run_quiet(add_invoice, *args))
        self.assertIsNone(self.run_quiet(add_invoice, *args))

    def test_same_file_hash_is_skipped(self):
        self.run_quiet(add_invoice, self.cur, self.conn, 1, self.period, "2026-05-12", "purchase", "S", "INV-5", "domestic",
                       [{"net_amount": 100, "vat_rate_id": STANDARD}], file_hash="abc123")
        again = self.run_quiet(add_invoice, self.cur, self.conn, 1, self.period, "2026-05-13", "purchase", "S", "INV-6", "domestic",
                               [{"net_amount": 100, "vat_rate_id": STANDARD}], file_hash="abc123")
        self.assertIsNone(again)

    def test_stated_vat_within_tolerance_is_kept(self):
        self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-10", "purchase", "S", 10.01,
                       STANDARD, "R-1", "domestic", stated_vat_amount=2.31)  # calculated 2.30
        self.assertEqual(self.cur.execute("SELECT vat_amount FROM transactions").fetchone()[0], 2.31)

    def test_stated_vat_outside_tolerance_is_rejected(self):
        with self.assertRaises(ValueError):
            add_transaction(self.cur, self.conn, 1, self.period, "2026-05-10", "purchase", "S", 100,
                            STANDARD, "R-2", "domestic", stated_vat_amount=19.00)
        self.assertEqual(self.count("transactions"), 0)

    def test_wrong_invoice_total_saves_nothing(self):
        with self.assertRaises(ValueError):
            add_invoice(self.cur, self.conn, 1, self.period, "2026-05-12", "purchase", "S", "INV-9", "domestic",
                        [{"net_amount": 100, "vat_rate_id": STANDARD}], stated_gross=150.00)
        self.assertEqual(self.count("invoices"), 0)
        self.assertEqual(self.count("transactions"), 0)

    # --- calculation -------------------------------------------------------
    def test_reverse_charge_purchase_is_neutral(self):
        self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-10", "purchase", "EU Supplier GmbH",
                       1000, STANDARD, "EU-1", "intra_eu", counterparty_vat_no="DE123456789", is_reverse_charge=True)
        boxes = calculate_vat3(self.cur, self.period)
        self.assertEqual((boxes["T1"], boxes["T2"], boxes["T3"], boxes["T4"], boxes["E2"]), (230.0, 230.0, 0.0, 0, 1000.0))

    def test_reverse_charge_flag_on_a_sale_does_not_inflate_t1(self):
        self.run_quiet(add_transaction, self.cur, self.conn, 1, self.period, "2026-05-10", "sale", "Customer",
                       1000, STANDARD, "S-1", "domestic", is_reverse_charge=True)
        self.assertEqual(calculate_vat3(self.cur, self.period)["T1"], 230.0)

    # --- migration ---------------------------------------------------------
    def test_old_database_is_upgraded_in_place(self):
        old = sqlite3.connect(":memory:")
        c = old.cursor()
        c.execute("CREATE TABLE vat_rates (rate_id INTEGER PRIMARY KEY, rate_name TEXT, rate_percentage DECIMAL, effective_from DATE, effective_to DATE)")
        c.execute("CREATE TABLE businesses (business_id INTEGER PRIMARY KEY, business_name TEXT, vat_registration_no TEXT, vat_period_type TEXT, accounting_basis TEXT)")
        c.execute("CREATE TABLE vat_periods (period_id INTEGER PRIMARY KEY, business_id INTEGER, period_start DATE, period_end DATE, due_date DATE, status TEXT DEFAULT 'open')")
        c.execute("CREATE TABLE transactions (transaction_id INTEGER PRIMARY KEY, business_id INTEGER, period_id INTEGER, transaction_date DATE, "
                  "transaction_type TEXT, counterparty_name TEXT, counterparty_vat_no TEXT, net_amount DECIMAL, vat_rate_id INTEGER, "
                  "vat_amount DECIMAL, gross_amount DECIMAL, invoice_number TEXT, supply_type TEXT, is_reverse_charge BOOLEAN DEFAULT 0)")
        c.execute("CREATE TABLE vat3_returns (return_id INTEGER PRIMARY KEY, period_id INTEGER, T1_vat_on_sales DECIMAL, T2_vat_on_purchases DECIMAL, "
                  "T3_vat_payable DECIMAL, T4_vat_repayable DECIMAL, E1_intra_eu_sales DECIMAL, E2_intra_eu_purchases DECIMAL, generated_at DATETIME)")
        create_tables(c)       # adds the new invoices table; leaves old tables as they are
        migrate_schema(c)
        migrate_schema(c)      # running twice must be harmless
        columns = [r[1] for r in c.execute("PRAGMA table_info(transactions)")]
        self.assertIn("invoice_id", columns)
        self.assertIn("stated_vat_amount", columns)


if __name__ == "__main__":
    unittest.main()
