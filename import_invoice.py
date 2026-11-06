"""
import_invoice.py
Ties the pieces together in two stages, so a human always checks the numbers:

  Stage A  stage_invoice():  photo -> extract -> map VAT rates -> validate -> review workbook (.xlsx)
  (you)    open the workbook, compare with the original invoice, correct, save
  Stage B  commit_invoice(): read the workbook -> validate again -> add_invoice() into the database

If period_id is None, the VAT period is found automatically from the invoice date.
For SALES invoices the counterparty is the customer ("Bill to"), not the issuer of the document.
"""

import json
import os

from invoice_extractor import extract_invoice
from invoice_validator import validate_invoice, has_errors
from invoice_review import export_review_workbook, import_review_workbook
from vat_calculator import add_invoice


def find_period(cursor, business_id, date):
    """Returns the period_id of the business whose dates cover `date` (YYYY-MM-DD), or None."""
    if not date:
        return None
    cursor.execute(
        "SELECT period_id FROM vat_periods WHERE business_id = ? AND period_start <= ? AND period_end >= ? "
        "ORDER BY period_id", (business_id, date, date))
    row = cursor.fetchone()
    return row[0] if row else None


def attach_rate_ids(cursor, data):
    """Maps each rate read from the invoice (e.g. 23.0) to a vat_rates.rate_id. 0% defaults to Zero Rated (5)."""
    for line in data.get("lines", []):
        rate = line.get("vat_rate")
        if rate is None:
            line["vat_rate_id"] = None
            continue
        cursor.execute("SELECT rate_id FROM vat_rates WHERE rate_percentage = ? ORDER BY rate_id", (rate,))
        matches = [row[0] for row in cursor.fetchall()]
        line["vat_rate_id"] = matches[0] if matches else None
        if len(matches) > 1:
            data.setdefault("extraction_notes", []).append(
                f"{rate}% matches several rate IDs {matches} (e.g. zero-rated vs exempt) - confirm the correct one")
    return data


def print_issues(issues):
    if not issues:
        print("Automatic checks: no problems found")
    for item in issues:
        print(f"  [{item['level']}] {item['field']}: {item['message']}")


def _no_period_issue(date):
    return {"level": "ERROR", "field": "invoice_date",
            "message": f"No VAT period covers {date} - create one first (menu option 7, or generate_vat_period)"}


def stage_invoice(cursor, image_path, business_id, period_id=None, review_path=None,
                  trans_type="purchase", supply_type="domestic", quiet=False):
    """Stage A. Returns (review_path, data, issues)."""
    data = extract_invoice(image_path)
    attach_rate_ids(cursor, data)
    data["trans_type"], data["supply_type"], data["is_reverse_charge"] = trans_type, supply_type, False

    if trans_type == "sale":                      # counterparty = the customer, not whoever issued the document
        issuer, customer = data.get("supplier"), data.get("bill_to")
        if customer:
            data["supplier"], data["supplier_vat_no"] = customer, None
            data["extraction_notes"].append(f"Sales invoice: counterparty set to customer '{customer}' (document issued by '{issuer}')")
        else:
            data["extraction_notes"].append("Sales invoice: no 'Bill to' found - type the customer name into the review sheet")

    period_missing = False
    if period_id is None:
        period_id = find_period(cursor, business_id, data.get("invoice_date"))
        period_missing = period_id is None and bool(data.get("invoice_date"))
    data["period_id"] = period_id

    issues = validate_invoice(data, cursor, business_id, period_id)
    if period_missing:
        issues.append(_no_period_issue(data["invoice_date"]))
    review_path = review_path or os.path.splitext(image_path)[0] + "_review.xlsx"
    export_review_workbook(data, issues, review_path)

    if not quiet:
        print(f"Read: {data.get('supplier')} | invoice {data.get('invoice_number')} | {data.get('invoice_date')} | "
              f"{len(data['lines'])} VAT line(s) | total EUR {data.get('stated_gross')} | period {period_id}")
        print_issues(issues)
        for note in data.get("extraction_notes", []):
            print(f"  [NOTE] {note}")
        print(f"Review workbook written: {review_path}")
    return review_path, data, issues


def commit_invoice(cursor, conn, business_id, period_id, review_path, quiet=False):
    """Stage B. Returns the new invoice_id, or None if it was not saved. period_id may be None (found from the date)."""
    data = import_review_workbook(review_path)
    if period_id is None:
        period_id = find_period(cursor, business_id, data.get("invoice_date"))
        if period_id is None:
            if not quiet:
                print("Not saved - " + _no_period_issue(data.get("invoice_date"))["message"])
            return None

    issues = validate_invoice(data, cursor, business_id, period_id)
    if has_errors(issues):
        if not quiet:
            print("Not saved - fix these errors in the review workbook, save it, and run this step again:")
            print_issues(issues)
        return None
    if not quiet:
        for item in issues:
            print(f"  [{item['level']}] {item['field']}: {item['message']}")

    audit = json.dumps({"reviewed": {k: v for k, v in data.items() if k not in ("file_hash",)},
                        "warnings": [i["message"] for i in issues]})
    lines = [{"net_amount": l["net_amount"], "vat_rate_id": l["vat_rate_id"],
              "stated_vat_amount": l["stated_vat_amount"]} for l in data["lines"]]
    return add_invoice(cursor, conn, business_id, period_id, data["invoice_date"], data["trans_type"],
                       data["supplier"], data["invoice_number"], data["supply_type"], lines,
                       counterparty_vat_no=data["supplier_vat_no"], is_reverse_charge=data["is_reverse_charge"],
                       stated_gross=data["stated_gross"], source_file=data["source_file"],
                       file_hash=data["file_hash"], extracted_json=audit)
