"""
vat_calculator.py
Core VAT logic for the Irish VAT3 compliance tool.

Contains:
- add_transaction(): records ONE sale/purchase line at a single VAT rate
- add_invoice(): records a whole invoice that may contain SEVERAL VAT rates
  (one invoice header + one transaction per rate, saved all-or-nothing)
- calculate_vat3(): aggregates a period's transactions into VAT3 boxes
- generate_vat3_report(): saves (or updates) the return and prints it

Notes:
- Duplicate check now uses supplier + invoice number + VAT rate (not invoice number alone)
- Transactions can only be added to an OPEN period that belongs to the business,
  and the date must fall inside the period
- VAT rate must have been in effect on the transaction date
- Optional `stated_vat_amount`: keep the VAT printed on the invoice, but reject it
  if it differs from the calculated VAT by more than VAT_TOLERANCE
- generate_vat3_report() updates the existing return instead of inserting a new row every run
- Reverse-charge VAT is only self-accounted on purchases (a sale flagged by mistake
  can no longer inflate T1)
"""

from datetime import datetime

VALID_TRANSACTION_TYPES = ("sale", "purchase")
VALID_SUPPLY_TYPES = ("domestic", "intra_eu", "export")
VAT_TOLERANCE = 0.05  # max € difference allowed between invoice VAT and calculated VAT (rounding)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_header(cursor, business_id, period_id, date, trans_type,
                     counterparty, invoice_number, supply_type):
    """Validates the fields shared by every line of an invoice. Returns cleaned values."""

    counterparty = (counterparty or "").strip()
    invoice_number = (invoice_number or "").strip()
    if not counterparty:
        raise ValueError("Counterparty name is required")
    if not invoice_number:
        raise ValueError("Invoice number is required")
    if trans_type not in VALID_TRANSACTION_TYPES:
        raise ValueError(f"Type must be one of {VALID_TRANSACTION_TYPES}, got '{trans_type}'")
    if supply_type not in VALID_SUPPLY_TYPES:
        raise ValueError(f"Supply type must be one of {VALID_SUPPLY_TYPES}, got '{supply_type}'")

    try:
        date = datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")  # normalises 2026-5-1 -> 2026-05-01
    except (TypeError, ValueError):
        raise ValueError("Date must be in YYYY-MM-DD format")

    cursor.execute('''
        SELECT business_id, period_start, period_end, status
        FROM vat_periods WHERE period_id = ?
    ''', (period_id,))
    period = cursor.fetchone()
    if period is None:
        raise ValueError(f"Period {period_id} does not exist")
    if period[0] != business_id:
        raise ValueError(f"Period {period_id} belongs to a different business")
    if period[3] != "open":
        raise ValueError(f"Period {period_id} is '{period[3]}' - transactions can only be added to open periods")
    if not (period[1] <= date <= period[2]):
        raise ValueError(f"Date {date} is outside period {period_id} ({period[1]} to {period[2]})")

    return date, counterparty, invoice_number


def _get_rate(cursor, vat_rate_id, date):
    """Looks up a VAT rate and checks it was in effect on the transaction date."""

    cursor.execute('''
        SELECT rate_percentage, effective_from, effective_to
        FROM vat_rates WHERE rate_id = ?
    ''', (vat_rate_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"VAT rate_id {vat_rate_id} not found in vat_rates table")
    rate, effective_from, effective_to = row
    if date < effective_from or (effective_to is not None and date > effective_to):
        raise ValueError(f"VAT rate {vat_rate_id} was not in effect on {date}")
    return rate


def _calculate_line(net_amount, rate, stated_vat_amount, reverse_charge=False):
    """
    Returns (net, vat, gross, stated).
    If the invoice's own VAT figure is supplied it is kept - provided it is within
    VAT_TOLERANCE of the calculated figure. Otherwise the calculated VAT is used.

    REVERSE CHARGE: the supplier charges no VAT, so the invoice must show none (0 or blank).
    The VAT is self-assessed at the Irish rate chosen for the line, and the cash payable
    (gross) is just the net amount.
    """

    net = round(float(net_amount), 2)
    calculated = round(net * (rate / 100), 2)

    if reverse_charge:
        if stated_vat_amount is not None and abs(float(stated_vat_amount)) > VAT_TOLERANCE:
            raise ValueError(
                f"Reverse-charge invoice shows VAT of €{float(stated_vat_amount):.2f} - the supplier should charge none")
        return net, calculated, net, None

    if stated_vat_amount is None:
        return net, calculated, round(net + calculated, 2), None

    stated = round(float(stated_vat_amount), 2)
    if abs(stated - calculated) > VAT_TOLERANCE:
        raise ValueError(
            f"VAT on the invoice (€{stated:.2f}) differs from the calculated VAT "
            f"(€{calculated:.2f} at {rate}%) by more than €{VAT_TOLERANCE:.2f}"
        )
    return net, stated, round(net + stated, 2), stated


def _transaction_exists(cursor, business_id, counterparty, invoice_number, vat_rate_id=None):
    """Duplicate check: same business + supplier (case-insensitive) + invoice number [+ rate]."""

    sql = '''
        SELECT 1 FROM transactions
        WHERE business_id = ? AND LOWER(TRIM(counterparty_name)) = LOWER(?) AND invoice_number = ?
    '''
    params = [business_id, counterparty, invoice_number]
    if vat_rate_id is not None:
        sql += " AND vat_rate_id = ?"
        params.append(vat_rate_id)
    cursor.execute(sql, params)
    return cursor.fetchone() is not None


def _insert_transaction(cursor, business_id, period_id, date, trans_type, counterparty,
                        counterparty_vat_no, net, vat_rate_id, vat, gross, invoice_number,
                        supply_type, is_reverse_charge, invoice_id, stated):
    """Inserts one transaction row. Does NOT commit - the caller decides when."""

    cursor.execute('''
        INSERT INTO transactions 
        (business_id, period_id, transaction_date, transaction_type,
         counterparty_name, counterparty_vat_no, net_amount, vat_rate_id,
         vat_amount, gross_amount, invoice_number, supply_type, is_reverse_charge,
         invoice_id, stated_vat_amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (business_id, period_id, date, trans_type, counterparty, counterparty_vat_no,
          net, vat_rate_id, vat, gross, invoice_number, supply_type,
          int(bool(is_reverse_charge)), invoice_id, stated))
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_transaction(cursor, conn, business_id, period_id, date, trans_type,
                    counterparty, net_amount, vat_rate_id,
                    invoice_number, supply_type,
                    counterparty_vat_no=None, is_reverse_charge=False,
                    stated_vat_amount=None):
    """
    Records a single transaction line (one VAT rate) and returns its transaction_id,
    or None if it was skipped as a duplicate.

    Raises ValueError for invalid input (bad date, closed period, unknown rate, ...).
    For an invoice with several VAT rates use add_invoice() instead.
    """

    date, counterparty, invoice_number = _validate_header(
        cursor, business_id, period_id, date, trans_type, counterparty, invoice_number, supply_type)
    rate = _get_rate(cursor, vat_rate_id, date)

    if _transaction_exists(cursor, business_id, counterparty, invoice_number, vat_rate_id):
        print(f"Skipped — invoice {invoice_number} from {counterparty} at this VAT rate already exists")
        return None

    net, vat, gross, stated = _calculate_line(net_amount, rate, stated_vat_amount, bool(is_reverse_charge))

    try:
        transaction_id = _insert_transaction(
            cursor, business_id, period_id, date, trans_type, counterparty, counterparty_vat_no,
            net, vat_rate_id, vat, gross, invoice_number, supply_type, is_reverse_charge, None, stated)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(f"Transaction added — {counterparty} | Net: €{net:.2f} | VAT: €{vat:.2f} | Gross: €{gross:.2f}")
    return transaction_id


def add_invoice(cursor, conn, business_id, period_id, date, trans_type,
                counterparty, invoice_number, supply_type, lines,
                counterparty_vat_no=None, is_reverse_charge=False,
                stated_gross=None, source_file=None, file_hash=None,
                extracted_json=None):
    """
    Records a whole invoice that may contain several VAT rates.

    `lines` is a list of dicts, one per line or rate group:
        {"net_amount": 100.00, "vat_rate_id": 1, "stated_vat_amount": 23.00}   # stated_vat_amount optional
    Lines with the same vat_rate_id are combined, so the result is one transaction per rate.

    The invoice header and all its transactions are saved together or not at all.
    Returns the new invoice_id, or None if the invoice (or the same file) was already imported.
    Raises ValueError for invalid input; nothing is saved in that case.
    """

    date, counterparty, invoice_number = _validate_header(
        cursor, business_id, period_id, date, trans_type, counterparty, invoice_number, supply_type)

    if not lines:
        raise ValueError("An invoice needs at least one line")

    # Combine lines that share a VAT rate
    grouped = {}
    for line in lines:
        if "net_amount" not in line or "vat_rate_id" not in line:
            raise ValueError("Each line needs 'net_amount' and 'vat_rate_id'")
        group = grouped.setdefault(line["vat_rate_id"], {"net": 0.0, "stated": 0.0, "has_stated": True})
        group["net"] += float(line["net_amount"])
        if line.get("stated_vat_amount") is None:
            group["has_stated"] = False
        else:
            group["stated"] += float(line["stated_vat_amount"])

    # Calculate and validate everything BEFORE touching the database
    prepared = []
    for vat_rate_id, group in grouped.items():
        rate = _get_rate(cursor, vat_rate_id, date)
        stated_in = round(group["stated"], 2) if group["has_stated"] else None
        net, vat, gross, stated = _calculate_line(group["net"], rate, stated_in, bool(is_reverse_charge))
        prepared.append((vat_rate_id, net, vat, gross, stated))

    total_gross = round(sum(p[3] for p in prepared), 2)   # for reverse charge this equals the net (cash payable)
    if stated_gross is not None and abs(round(float(stated_gross), 2) - total_gross) > VAT_TOLERANCE:
        raise ValueError(
            f"Invoice total on the document (€{float(stated_gross):.2f}) does not match "
            f"the sum of the lines (€{total_gross:.2f})"
        )

    # Duplicate checks
    if file_hash:
        cursor.execute("SELECT 1 FROM invoices WHERE business_id = ? AND file_hash = ?", (business_id, file_hash))
        if cursor.fetchone():
            print("Skipped — this file has already been imported")
            return None
    cursor.execute('''
        SELECT 1 FROM invoices
        WHERE business_id = ? AND LOWER(TRIM(counterparty_name)) = LOWER(?) AND invoice_number = ?
    ''', (business_id, counterparty, invoice_number))
    if cursor.fetchone() or _transaction_exists(cursor, business_id, counterparty, invoice_number):
        print(f"Skipped — invoice {invoice_number} from {counterparty} already exists")
        return None

    try:
        cursor.execute('''
            INSERT INTO invoices
            (business_id, period_id, counterparty_name, counterparty_vat_no, invoice_number,
             invoice_date, gross_total, source_file, file_hash, status, extracted_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?)
        ''', (business_id, period_id, counterparty, counterparty_vat_no, invoice_number,
              date, total_gross, source_file, file_hash, extracted_json))
        invoice_id = cursor.lastrowid

        for vat_rate_id, net, vat, gross, stated in prepared:
            _insert_transaction(
                cursor, business_id, period_id, date, trans_type, counterparty, counterparty_vat_no,
                net, vat_rate_id, vat, gross, invoice_number, supply_type, is_reverse_charge,
                invoice_id, stated)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    total_net = round(sum(p[1] for p in prepared), 2)
    total_vat = round(sum(p[2] for p in prepared), 2)
    print(f"Invoice added — {counterparty} #{invoice_number} | {len(prepared)} VAT rate(s) | "
          f"Net: €{total_net:.2f} | VAT: €{total_vat:.2f} | Gross: €{total_gross:.2f}")
    return invoice_id


def calculate_vat3(cursor, period_id):
    """
    Aggregates all transactions in a given period into VAT3 return boxes.

    T1 - VAT on sales (output VAT), including reverse charge VAT
    T2 - VAT on purchases (input VAT), including reverse charge VAT
    T3 - Net VAT payable to Revenue (T1 - T2, if positive)
    T4 - Net VAT repayable by Revenue (T2 - T1, if positive)
    E1 - Value of intra-EU sales (informational)
    E2 - Value of intra-EU purchases (informational)

    NOTE (to review in a later step): on the Revenue VAT3 form, E1/E2 cover GOODS only;
    services have separate boxes (ES1/ES2). supply_type does not yet distinguish the two.
    """

    cursor.execute('''
        SELECT SUM(vat_amount) FROM transactions
        WHERE period_id = ? AND transaction_type = 'sale'
    ''', (period_id,))
    t1 = cursor.fetchone()[0] or 0

    cursor.execute('''
        SELECT SUM(vat_amount) FROM transactions
        WHERE period_id = ? AND transaction_type = 'purchase'
    ''', (period_id,))
    t2 = cursor.fetchone()[0] or 0

    # Reverse charge VAT self-accounted on purchases also counts toward T1
    # (it is already inside T2 above, so the net cash effect is nil).
    cursor.execute('''
        SELECT SUM(vat_amount) FROM transactions
        WHERE period_id = ? AND is_reverse_charge = 1 AND transaction_type = 'purchase'
    ''', (period_id,))
    reverse_charge_vat = cursor.fetchone()[0] or 0

    t1 = t1 + reverse_charge_vat

    if t1 >= t2:
        t3 = round(t1 - t2, 2)
        t4 = 0
    else:
        t3 = 0
        t4 = round(t2 - t1, 2)

    cursor.execute('''
        SELECT SUM(net_amount) FROM transactions
        WHERE period_id = ? AND supply_type = 'intra_eu' AND transaction_type = 'purchase'
    ''', (period_id,))
    e2 = cursor.fetchone()[0] or 0

    cursor.execute('''
        SELECT SUM(net_amount) FROM transactions
        WHERE period_id = ? AND supply_type = 'intra_eu' AND transaction_type = 'sale'
    ''', (period_id,))
    e1 = cursor.fetchone()[0] or 0

    return {
        "T1": round(t1, 2),
        "T2": round(t2, 2),
        "T3": t3,
        "T4": t4,
        "E1": round(e1, 2),
        "E2": round(e2, 2)
    }


def generate_vat3_report(cursor, conn, period_id, business_id):
    """
    Calculates the VAT3 return for a period, saves it to vat3_returns
    (updating the existing row if one exists) and prints a formatted report.
    """

    cursor.execute("SELECT business_name, vat_registration_no FROM businesses WHERE business_id = ?", (business_id,))
    biz = cursor.fetchone()
    if biz is None:
        raise ValueError(f"Business {business_id} does not exist")

    cursor.execute('''
        SELECT period_start, period_end, due_date, status, business_id
        FROM vat_periods WHERE period_id = ?
    ''', (period_id,))
    period = cursor.fetchone()
    if period is None:
        raise ValueError(f"Period {period_id} does not exist")
    if period[4] != business_id:
        raise ValueError(f"Period {period_id} belongs to a different business")

    result = calculate_vat3(cursor, period_id)

    cursor.execute("SELECT return_id FROM vat3_returns WHERE period_id = ?", (period_id,))
    existing = cursor.fetchone()
    values = (result["T1"], result["T2"], result["T3"], result["T4"], result["E1"], result["E2"])

    if existing:
        cursor.execute('''
            UPDATE vat3_returns
            SET T1_vat_on_sales = ?, T2_vat_on_purchases = ?, T3_vat_payable = ?,
                T4_vat_repayable = ?, E1_intra_eu_sales = ?, E2_intra_eu_purchases = ?,
                generated_at = datetime('now')
            WHERE return_id = ?
        ''', values + (existing[0],))
    else:
        cursor.execute('''
            INSERT INTO vat3_returns 
            (period_id, T1_vat_on_sales, T2_vat_on_purchases, T3_vat_payable, 
             T4_vat_repayable, E1_intra_eu_sales, E2_intra_eu_purchases, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ''', (period_id,) + values)

    conn.commit()

    print("=" * 50)
    print(f"VAT3 RETURN — {biz[0]}")
    print(f"VAT Reg No: {biz[1]}")
    print(f"Period: {period[0]} to {period[1]}  ({period[3]})")
    print(f"Due Date: {period[2]}")
    print("=" * 50)
    print(f"T1  VAT on Sales:          €{result['T1']:.2f}")
    print(f"T2  VAT on Purchases:      €{result['T2']:.2f}")
    print(f"T3  Net Payable:           €{result['T3']:.2f}")
    print(f"T4  Net Repayable:         €{result['T4']:.2f}")
    print(f"E1  Intra-EU Sales:        €{result['E1']:.2f}")
    print(f"E2  Intra-EU Purchases:    €{result['E2']:.2f}")
    print("=" * 50)

    return result
