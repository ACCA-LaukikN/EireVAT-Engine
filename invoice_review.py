"""
invoice_review.py
The human-review loop: writes the extracted invoice to an Excel sheet, you correct it,
and this module reads the corrected values back.

Blue cells are inputs you can edit. The Calculated VAT and Check columns are live
formulas, so you see straight away whether your correction fixes the problem.
"""

from datetime import datetime, date
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.datavalidation import DataValidation

FIRST_LINE_ROW = 14
MAX_LINES = 10
LAST_LINE_ROW = FIRST_LINE_ROW + MAX_LINES - 1
FONT = "Arial"
BLUE, NAVY = "0000FF", "1F3864"
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")


def _font(**kw):
    return Font(name=FONT, size=10, **kw)


def export_review_workbook(data, issues, path):
    """Writes the review workbook. Returns the path."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Review"
    ws.sheet_view.showGridLines = False

    ws["A1"] = "Invoice review – check every yellow cell against the original document"
    ws["A1"].font = Font(name=FONT, size=14, bold=True, color=NAVY)
    ws["A2"] = "Fix anything wrong, save this file, then run the commit step. Errors on the Issues tab must be fixed first."
    ws["A2"].font = _font(italic=True, color="595959")

    header_fields = [
        ("Supplier", data.get("supplier")),
        ("Supplier VAT No", data.get("supplier_vat_no")),
        ("Invoice No", data.get("invoice_number")),
        ("Invoice date (YYYY-MM-DD)", data.get("invoice_date")),
        ("Type (sale/purchase)", data.get("trans_type", "purchase")),
        ("Supply type (domestic/intra_eu/export)", data.get("supply_type", "domestic")),
        ("Reverse charge (Y/N)", "Y" if data.get("is_reverse_charge") else "N"),
        ("Invoice total on document (€)", data.get("stated_gross")),
    ]
    for offset, (label, value) in enumerate(header_fields):
        row = 3 + offset
        ws.cell(row, 1, label).font = _font(bold=True)
        cell = ws.cell(row, 2, value)
        cell.font = _font(color=BLUE)
        cell.fill = INPUT_FILL
    ws["B10"].number_format = "#,##0.00"

    dv_type = DataValidation(type="list", formula1='"sale,purchase"'); ws.add_data_validation(dv_type); dv_type.add("B7")
    dv_supply = DataValidation(type="list", formula1='"domestic,intra_eu,export"'); ws.add_data_validation(dv_supply); dv_supply.add("B8")
    dv_rc = DataValidation(type="list", formula1='"Y,N"'); ws.add_data_validation(dv_rc); dv_rc.add("B9")

    headers = ["VAT rate %", "VAT rate ID", "Net (€)", "VAT on invoice (€)", "Calculated VAT (€)", "Check", "VAT used (€)"]
    for col, text in enumerate(headers, start=1):
        c = ws.cell(FIRST_LINE_ROW - 1, col, text)
        c.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    ws.cell(FIRST_LINE_ROW - 2, 1, "VAT lines (one row per VAT rate; spare rows below are for extra rates)").font = _font(bold=True, color=NAVY)

    rows = data.get("lines") or []
    for i in range(MAX_LINES):
        r = FIRST_LINE_ROW + i
        line = rows[i] if i < len(rows) else {}
        for col, key in ((1, "vat_rate"), (2, "vat_rate_id"), (3, "net_amount"), (4, "stated_vat_amount")):
            c = ws.cell(r, col, line.get(key))
            c.font = _font(color=BLUE); c.fill = INPUT_FILL
        ws.cell(r, 5, f'=IF(C{r}="","",ROUND(C{r}*A{r}/100,2))')
        ws.cell(r, 6, f'=IF(C{r}="","",IF(D{r}="","calc only",IF(ABS(D{r}-E{r})<=0.05,"OK","CHECK")))')
        ws.cell(r, 7, f'=IF(C{r}="","",IF(D{r}="",E{r},D{r}))')
        for col in (3, 4, 5, 7):
            ws.cell(r, col).number_format = "#,##0.00"
        for col in (5, 6, 7):
            ws.cell(r, col).font = _font()

    t = LAST_LINE_ROW + 2
    ws.cell(t, 1, "Totals").font = _font(bold=True)
    ws.cell(t, 3, f"=SUM(C{FIRST_LINE_ROW}:C{LAST_LINE_ROW})")
    ws.cell(t, 7, f"=SUM(G{FIRST_LINE_ROW}:G{LAST_LINE_ROW})")
    ws.cell(t + 1, 1, "Lines total (net + VAT)").font = _font(bold=True)
    ws.cell(t + 1, 3, f"=C{t}+G{t}")
    ws.cell(t + 1, 4, '=IF(B10="","no document total",IF(ABS(B10-C%d)<=0.05,"MATCHES document total","DOES NOT MATCH document total"))' % (t + 1))
    for r in (t, t + 1):
        for col in (3, 7):
            ws.cell(r, col).number_format = "#,##0.00"
            ws.cell(r, col).font = _font(bold=True)
        ws.cell(r, 4).font = _font(bold=True)

    for col, width in zip("ABCDEFG", (38, 28, 14, 20, 20, 12, 14)):
        ws.column_dimensions[col].width = width

    # Issues tab
    iss = wb.create_sheet("Issues")
    iss.append(["Level", "Field", "Message"])
    for c in iss[1]:
        c.font = Font(name=FONT, bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor=NAVY)
    if not issues:
        iss.append(["OK", "", "No problems found by the automatic checks (still compare with the original)"])
    for item in issues:
        iss.append([item["level"], item["field"], item["message"]])
    for note in data.get("extraction_notes") or []:
        iss.append(["NOTE", "extraction", note])
    for row in iss.iter_rows(min_row=2):
        for c in row:
            c.font = _font(color="C00000" if row[0].value == "ERROR" else "000000")
    iss.column_dimensions["A"].width = 10; iss.column_dimensions["B"].width = 20; iss.column_dimensions["C"].width = 110

    # Raw OCR text and meta
    raw = wb.create_sheet("Raw OCR text")
    for i, line in enumerate((data.get("raw_text") or "").splitlines(), start=1):
        raw.cell(i, 1, line).font = Font(name="Courier New", size=10)
    raw.column_dimensions["A"].width = 120

    meta = wb.create_sheet("Meta")
    meta["A1"], meta["B1"] = "source_file", data.get("source_file")
    meta["A2"], meta["B2"] = "file_hash", data.get("file_hash")
    meta["A3"], meta["B3"] = "created", datetime.now().strftime("%Y-%m-%d %H:%M")
    meta["A5"] = "Do not edit this tab – it links the review sheet back to the original file."
    meta["A5"].font = _font(italic=True)
    meta.column_dimensions["A"].width = 14; meta.column_dimensions["B"].width = 70

    wb.save(path)
    return path


def _text(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    value = str(value).strip()
    return value or None


def _number(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("€", "").replace(",", "").replace(" ", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return float(cleaned)


def import_review_workbook(path):
    """Reads the corrected values back from the review workbook into the same dict shape the validator expects."""
    wb = load_workbook(path, data_only=False)
    ws = wb["Review"]
    meta = wb["Meta"] if "Meta" in wb.sheetnames else None

    lines = []
    for r in range(FIRST_LINE_ROW, LAST_LINE_ROW + 1):
        net = _number(ws.cell(r, 3).value)
        if net is None:
            continue
        rate_id = _number(ws.cell(r, 2).value)
        lines.append({
            "vat_rate": _number(ws.cell(r, 1).value),
            "vat_rate_id": int(rate_id) if rate_id is not None else None,
            "net_amount": net,
            "stated_vat_amount": _number(ws.cell(r, 4).value),
        })

    return {
        "supplier": _text(ws["B3"].value),
        "supplier_vat_no": _text(ws["B4"].value),
        "invoice_number": _text(ws["B5"].value),
        "invoice_date": _text(ws["B6"].value),
        "trans_type": (_text(ws["B7"].value) or "purchase").lower(),
        "supply_type": (_text(ws["B8"].value) or "domestic").lower(),
        "is_reverse_charge": (_text(ws["B9"].value) or "N").upper() == "Y",
        "stated_gross": _number(ws["B10"].value),
        "lines": lines,
        "source_file": _text(meta["B1"].value) if meta else None,
        "file_hash": _text(meta["B2"].value) if meta else None,
    }
