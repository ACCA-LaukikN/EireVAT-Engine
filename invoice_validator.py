"""
invoice_validator.py
Checks extracted (or hand-corrected) invoice data BEFORE anything is saved.

Each problem is returned as {"level": "ERROR" | "WARNING", "field": ..., "message": ...}.
- ERROR   = must be fixed; the invoice will not be saved.
- WARNING = worth a look (possible OCR slip, credit note, missing VAT number).
"""

import re
from datetime import datetime, date

from vat_calculator import VAT_TOLERANCE, _transaction_exists

# 7 digits + 1-2 letters, or the older digit/letter/5 digits/letter layout (format check only, no check-letter test)
IE_VAT_FORMAT = re.compile(r"^IE(\d{7}[A-W][A-IW]?|\d[A-Z+*]\d{5}[A-W])$")


def _issue(level, field, message):
    return {"level": level, "field": field, "message": message}


def _rate_for_line(line, cursor):
    """Rate % used for the VAT check: the DB rate for vat_rate_id when available, else the rate read from the invoice."""
    if cursor is not None and line.get("vat_rate_id") is not None:
        cursor.execute("SELECT rate_percentage FROM vat_rates WHERE rate_id = ?", (line["vat_rate_id"],))
        row = cursor.fetchone()
        return row[0] if row else None
    return line.get("vat_rate")


def validate_invoice(data, cursor=None, business_id=None, period_id=None):
    """Returns a list of issues. An empty list means the invoice passed every check."""
    issues = []

    # --- header fields ---------------------------------------------------
    if not data.get("supplier"):
        issues.append(_issue("ERROR", "supplier", "Supplier name is missing"))
    if not data.get("invoice_number"):
        issues.append(_issue("ERROR", "invoice_number", "Invoice number is missing"))

    invoice_date = data.get("invoice_date")
    parsed = None
    if not invoice_date:
        issues.append(_issue("ERROR", "invoice_date", "Invoice date is missing"))
    else:
        try:
            parsed = datetime.strptime(invoice_date, "%Y-%m-%d").date()
        except ValueError:
            issues.append(_issue("ERROR", "invoice_date", f"Date '{invoice_date}' is not in YYYY-MM-DD format"))
    if parsed and parsed > date.today():
        issues.append(_issue("WARNING", "invoice_date", f"Invoice date {invoice_date} is in the future"))

    if parsed and cursor is not None and period_id is not None:
        cursor.execute("SELECT period_start, period_end FROM vat_periods WHERE period_id = ?", (period_id,))
        period = cursor.fetchone()
        if period and not (period[0] <= invoice_date <= period[1]):
            issues.append(_issue("ERROR", "invoice_date", f"Date {invoice_date} is outside period {period_id} ({period[0]} to {period[1]})"))

    vat_no = (data.get("supplier_vat_no") or "").replace(" ", "").upper()
    if not vat_no:
        issues.append(_issue("WARNING", "supplier_vat_no", "No supplier VAT number - a valid VAT invoice normally shows one"))
    elif not IE_VAT_FORMAT.match(vat_no) and vat_no.startswith("IE"):
        issues.append(_issue("WARNING", "supplier_vat_no", f"'{vat_no}' does not look like a valid Irish VAT number - check the characters"))

    # --- lines -----------------------------------------------------------
    lines = data.get("lines") or []
    if not lines:
        issues.append(_issue("ERROR", "lines", "No VAT lines found - add at least one net amount with a VAT rate"))

    reverse_charge = bool(data.get("is_reverse_charge"))
    sum_net = sum_vat = 0.0
    for number, line in enumerate(lines, start=1):
        label = f"line {number}"
        net, stated = line.get("net_amount"), line.get("stated_vat_amount")
        if net is None:
            issues.append(_issue("ERROR", label, "Net amount is missing"))
            continue
        if net < 0:
            issues.append(_issue("WARNING", label, "Negative net amount - treated as a credit note"))
        if line.get("vat_rate_id") is None:
            issues.append(_issue("ERROR", label, "No VAT rate ID - pick one of the rates in the vat_rates table"))
            continue
        rate = _rate_for_line(line, cursor)
        if rate is None:
            issues.append(_issue("ERROR", label, f"VAT rate ID {line['vat_rate_id']} does not exist"))
            continue
        calculated = round(net * rate / 100, 2)
        if reverse_charge:
            if stated is not None and abs(stated) > VAT_TOLERANCE:
                issues.append(_issue("ERROR", label, f"Reverse-charge invoice shows VAT of €{stated:.2f} - the supplier should charge none"))
            if rate == 0:
                issues.append(_issue("WARNING", label, "Reverse charge with a 0% rate gives no self-assessed VAT - choose the Irish rate that applies (e.g. 23%)"))
            sum_net += net          # document shows net only; self-assessed VAT is not on the document
            continue
        if stated is not None and abs(stated - calculated) > VAT_TOLERANCE:
            issues.append(_issue("ERROR", label, f"VAT on invoice €{stated:.2f} does not match €{calculated:.2f} calculated at {rate}% (check the net amount, rate or OCR digits)"))
        sum_net += net
        sum_vat += stated if stated is not None else calculated

    # --- totals ------------------------------------------------------------
    if lines:
        expected_vat = 0.0 if reverse_charge else sum_vat
        for key, label, value in (("stated_net", "total net", sum_net), ("stated_vat", "total VAT", expected_vat),
                                  ("stated_gross", "invoice total", sum_net + expected_vat)):
            stated_total = data.get(key)
            if stated_total is not None and abs(stated_total - value) > VAT_TOLERANCE:
                issues.append(_issue("ERROR", key, f"{label} on document €{stated_total:.2f} does not match the lines (€{value:.2f})"))

    # --- duplicates ----------------------------------------------------------
    if cursor is not None and business_id is not None:
        if data.get("supplier") and data.get("invoice_number"):
            cursor.execute('''SELECT 1 FROM invoices WHERE business_id = ? AND LOWER(TRIM(counterparty_name)) = LOWER(?)
                              AND invoice_number = ?''', (business_id, data["supplier"].strip(), data["invoice_number"].strip()))
            if cursor.fetchone() or _transaction_exists(cursor, business_id, data["supplier"].strip(), data["invoice_number"].strip()):
                issues.append(_issue("ERROR", "invoice_number", "This supplier and invoice number already exist in the database"))
        if data.get("file_hash"):
            cursor.execute("SELECT 1 FROM invoices WHERE business_id = ? AND file_hash = ?", (business_id, data["file_hash"]))
            if cursor.fetchone():
                issues.append(_issue("ERROR", "file", "This exact file has already been imported"))

    return issues


def has_errors(issues):
    return any(item["level"] == "ERROR" for item in issues)
