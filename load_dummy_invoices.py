"""
load_dummy_invoices.py
Loads the dummy invoices in sample_data/dummy_invoices.csv straight into vat3.db through add_invoice(),
so every rule (period checks, duplicates, VAT tolerance, reverse charge) still applies.

You can open the CSV in Excel and edit it. One row per VAT rate; rows with the
same file + invoice number form ONE invoice. Columns:
  file, type (sale/purchase), counterparty, counterparty_vat_no, invoice_number, date (YYYY-MM-DD),
  supply_type (domestic/intra_eu/export), reverse_charge (Y/N), vat_rate_id, net_amount,
  vat_on_invoice (blank for reverse charge), doc_total (total on the document), note

Run with:  python load_dummy_invoices.py            (or: python load_dummy_invoices.py my_file.csv)
Safe to run twice: invoices already in the database are skipped.
"""

import csv
import os
import sys
from datetime import datetime

from db_setup import initialise_database
from import_invoice import find_period
from period_manager import generate_vat_period
from vat_calculator import add_invoice, calculate_vat3

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(HERE, "sample_data", "dummy_invoices.csv")
BUSINESS_NAME = "Demo Traders Ltd"


def get_or_create_business(cursor, conn):
    cursor.execute("SELECT business_id FROM businesses WHERE business_name = ?", (BUSINESS_NAME,))
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute('''INSERT INTO businesses (business_name, vat_registration_no, vat_period_type, accounting_basis)
                      VALUES (?, 'IE1234567T', 'bi-monthly', 'invoice')''', (BUSINESS_NAME,))
    conn.commit()
    return cursor.lastrowid


def get_or_create_period(cursor, conn, business_id, date):
    """Finds the VAT period covering `date`, creating the bi-monthly period (Jan-Feb, Mar-Apr, ...) if needed."""
    period_id = find_period(cursor, business_id, date)
    if period_id:
        return period_id
    d = datetime.strptime(date, "%Y-%m-%d")
    start_month = d.month if d.month % 2 == 1 else d.month - 1
    return generate_vat_period(cursor, conn, business_id, f"{d.year}-{start_month:02d}-01")


def number(text):
    text = (text or "").strip()
    return float(text) if text else None


def read_invoices(path):
    """Groups CSV rows into invoices (one per file + invoice number)."""
    invoices = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (row["file"], row["invoice_number"])
            inv = invoices.setdefault(key, {**row, "lines": []})
            inv["lines"].append({"net_amount": float(row["net_amount"]), "vat_rate_id": int(row["vat_rate_id"]),
                                 "stated_vat_amount": number(row["vat_on_invoice"])})
    return list(invoices.values())


def main(csv_path=DEFAULT_CSV, db_name="vat3.db"):
    conn, cursor = initialise_database(db_name, quiet=True)
    business_id = get_or_create_business(cursor, conn)
    invoices = read_invoices(csv_path)

    loaded, skipped, failed, periods = 0, 0, [], set()
    for inv in invoices:
        label = f"{inv['file']} | {inv['counterparty']} #{inv['invoice_number']}"
        try:
            period_id = get_or_create_period(cursor, conn, business_id, inv["date"])
            invoice_id = add_invoice(
                cursor, conn, business_id, period_id, inv["date"], inv["type"], inv["counterparty"],
                inv["invoice_number"], inv["supply_type"], inv["lines"],
                counterparty_vat_no=inv["counterparty_vat_no"] or None,
                is_reverse_charge=inv["reverse_charge"].strip().upper() == "Y",
                stated_gross=number(inv["doc_total"]), source_file=inv["file"])
            periods.add(period_id)
            if invoice_id:
                loaded += 1
            else:
                skipped += 1
        except ValueError as error:
            failed.append((label, str(error)))

    print(f"\nLoaded {loaded} invoice(s); skipped {skipped} duplicate(s); {len(failed)} failed.")
    for label, reason in failed:
        print(f"  FAILED {label}: {reason}")

    for period_id in sorted(periods):
        cursor.execute("SELECT period_start, period_end FROM vat_periods WHERE period_id = ?", (period_id,))
        start, end = cursor.fetchone()
        boxes = calculate_vat3(cursor, period_id)
        print(f"\nPeriod {period_id} ({start} to {end}):  " + "  ".join(f"{k} {v:,.2f}" for k, v in boxes.items()))
    print(f"\nBusiness ID: {business_id}. Use option 3 or 4 in main.py (or calculate_vat3) to look at any period.")
    return conn, cursor


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV)
