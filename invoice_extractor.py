"""
invoice_extractor.py
Turns a photo/scan of an invoice into structured data.

Pipeline:  image -> clean-up (Pillow) -> OCR (Tesseract) -> rule-based parsing -> dict

IMPORTANT: this parser is rule-based and was tuned on the sample layouts in
./sample_invoices. Real supplier invoices vary, so ALWAYS review the result
(that is what the Excel review step is for). Never post OCR output unchecked.

Needs: Pillow, pytesseract and the Tesseract program itself
  - Colab:   !apt-get -qq install tesseract-ocr   and   !pip install pytesseract
  - Windows: install Tesseract from the UB Mannheim build, then pip install pytesseract
"""

import hashlib
import re
from datetime import datetime

# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def file_sha256(path):
    """Fingerprint of the file, used to stop the same photo being imported twice."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ocr_image(path):
    """Cleans up the image and returns the text Tesseract reads from it."""
    try:
        import pytesseract
        from PIL import Image, ImageFilter, ImageOps
    except ImportError as error:
        raise RuntimeError("Install the OCR libraries first:  pip install pytesseract pillow") from error

    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("L")        # fix phone rotation, greyscale
    img = ImageOps.autocontrast(img, cutoff=1)              # stronger contrast
    if img.width < 1800:                                    # small photos OCR badly - enlarge
        scale = 1800 / img.width
        img = img.resize((1800, int(img.height * scale)), Image.LANCZOS)
    img = img.filter(ImageFilter.SHARPEN)

    def read(image):
        """Returns (text, mean word confidence)."""
        data = pytesseract.image_to_data(image, config="--psm 6", output_type=pytesseract.Output.DICT)
        scores = [float(c) for w, c in zip(data["text"], data["conf"]) if w.strip() and float(c) >= 0]
        return pytesseract.image_to_string(image, config="--psm 6"), (sum(scores) / len(scores) if scores else 0.0)

    try:
        text, confidence = read(img)
    except pytesseract.TesseractNotFoundError as error:
        raise RuntimeError("Tesseract is not installed (see the note at the top of invoice_extractor.py)") from error

    # Orientation detection can misfire on pages with little text, so a rotated
    # version is only used if Tesseract reads it with higher confidence.
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        rotation = int(osd.get("rotate", 0))
        if rotation:
            rotated_text, rotated_confidence = read(img.rotate(-rotation, expand=True, fillcolor=255))
            if rotated_confidence > confidence:
                text = rotated_text
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

AMOUNT_RE = re.compile(r"(?<![\d.])(-?\d{1,3}(?:,\d{3})+\.\d{2}|-?\d+\.\d{2})(?!\d)")
RATE_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s?%")
INVOICE_NO_RE = re.compile(
    r"(?:tax\s+)?invoice\s*(?:no\.?|number|num\.?|#|ref\.?)\s*[:#.\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/_.]{1,24})", re.I)
# Irish VAT numbers: 7 digits + 1-2 letters (modern) or digit/letter/5 digits/letter (older)
IE_VAT_RE = re.compile(r"\b([I1l]E)\s?(\d{7}[A-W][A-IW]?|\d[A-Z+*]\d{5}[A-W])\b")
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b"), "dMy"),
]
UNLABELLED_NO_RE = re.compile(r"^[A-Za-z]{2,6}[-/][A-Za-z0-9][A-Za-z0-9\-/]{1,20}$")
GENERIC_FIRST_LINES = ("invoice", "tax invoice", "receipt", "credit note", "statement")


def to_number(text):
    return float(text.replace(",", ""))


def parse_date(text):
    """Finds the first date in `text` and returns it as YYYY-MM-DD (or None). Irish day-first order."""
    for pattern, kind in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        a, b, c = match.groups()
        try:
            if kind == "ymd":
                return datetime(int(a), int(b), int(c)).strftime("%Y-%m-%d")
            if kind == "dmy":
                year = int(c) + 2000 if len(c) == 2 else int(c)
                return datetime(year, int(b), int(a)).strftime("%Y-%m-%d")
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(f"{a} {b} {c}", fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
        except ValueError:
            continue
    return None


def _find_invoice_date(lines):
    for line in lines:
        low = line.lower()
        if "due" in low:
            continue
        if low.strip().startswith("date") or "invoice date" in low or "date of issue" in low:
            found = parse_date(line)
            if found:
                return found
    for line in lines:                       # fallback: first date anywhere that is not a due date
        if "due" not in line.lower():
            found = parse_date(line)
            if found:
                return found
    return None


def _last_amount(line):
    amounts = AMOUNT_RE.findall(line)
    return to_number(amounts[-1]) if amounts else None


def _find_total(lines, include, exclude=()):
    for line in lines:
        low = line.lower()
        if any(word in low for word in include) and not any(word in low for word in exclude):
            value = _last_amount(line)
            if value is not None:
                return value
    return None


def _rate_lines(lines):
    """
    Reads the VAT breakdown. Preferred source is a VAT summary row such as '23%  100.00  23.00'
    (rate, net, VAT). If there is none, falls back to item lines ('... 45.00  23%') grouped by rate.
    Returns (list of {vat_rate, net_amount, stated_vat_amount}, note or None).
    """
    summary = []
    singles = []                                  # lines like "VAT 23% 195.50": rate and VAT but no net
    for line in lines:
        match = RATE_RE.search(line)
        if not match:
            continue
        after = AMOUNT_RE.findall(line[match.end():])
        if not after:
            continue
        rate = float(match.group(1))
        if len(after) == 1:
            if "vat" in line.lower():
                singles.append((rate, to_number(after[0])))      # "VAT 23% 195.50" / "zero-rated VAT 0% 0.00"
            elif rate == 0:
                summary.append({"vat_rate": rate, "net_amount": to_number(after[0]), "stated_vat_amount": 0.0})
            continue
        first, second = to_number(after[0]), to_number(after[1])
        net, vat = first, second
        if abs(first * rate / 100 - second) > 0.06 and abs(second * rate / 100 - first) <= 0.06:
            net, vat = second, first         # columns were printed VAT first, net second
        summary.append({"vat_rate": rate, "net_amount": net, "stated_vat_amount": vat})
    if summary:
        return summary, None, singles

    grouped = {}
    for line in lines:
        match = RATE_RE.search(line)
        if not match:
            continue
        before = AMOUNT_RE.findall(line[:match.start()])
        if before and not AMOUNT_RE.findall(line[match.end():]):
            rate = float(match.group(1))
            grouped[rate] = round(grouped.get(rate, 0.0) + to_number(before[-1]), 2)
    rows = [{"vat_rate": rate, "net_amount": net, "stated_vat_amount": None} for rate, net in grouped.items()]
    note = "No VAT summary found - net amounts were added up from item lines and VAT is calculated, not read" if rows else None
    return rows, note, singles


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_invoice_text(text):
    """Turns OCR text into a dict. Notes list anything the reviewer should look at."""
    text = re.sub(r"(\d{1,2}(?:\.\d{1,2})?)\s*percent", r"\1%", text, flags=re.I)   # "23 percent" -> "23%"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    notes = []

    supplier = next((l for l in lines if len(re.findall(r"[A-Za-z]", l)) >= 3
                     and l.lower() not in GENERIC_FIRST_LINES), None)

    inv = INVOICE_NO_RE.search(text)
    invoice_number = inv.group(1).rstrip(".,;:") if inv else None
    if not invoice_number:                       # unlabelled number on its own line, e.g. GH-2026-102
        for line in lines:
            if UNLABELLED_NO_RE.match(line):
                invoice_number = line
                notes.append(f"Invoice number '{line}' taken from an unlabelled line - please verify")
                break

    vat_no = None
    vat_match = IE_VAT_RE.search(text)
    if vat_match:
        vat_no = "IE" + vat_match.group(2)
        if vat_match.group(1) != "IE":
            notes.append(f"OCR read the VAT number prefix as '{vat_match.group(1)}' - changed to 'IE', please verify")

    bill = re.search(r"(?:bill(?:ed)?\s*to|sold\s*to|customer)\s*[:\-]\s*(.+)", text, re.I)
    bill_to = bill.group(1).strip() if bill else None

    rows, rate_note, singles = _rate_lines(lines)
    if rate_note:
        notes.append(rate_note)

    data = {
        "supplier": supplier,
        "supplier_vat_no": vat_no,
        "invoice_number": invoice_number,
        "invoice_date": _find_invoice_date(lines),
        "bill_to": bill_to,
        "lines": rows,
        "stated_net": _find_total(lines, ("total net", "net total", "subtotal", "sub-total", "sub total", "net amount")),
        "stated_vat": _find_total(lines, ("total vat", "vat total", "vat amount", "total tax"), exclude=("%",)),
        "stated_gross": _find_total(lines, ("total due", "amount due", "balance due", "grand total",
                                            "total payable", "invoice total", "gross total", "total gross")),
        "extraction_notes": notes,
    }
    if data["stated_net"] is None:               # a line that is just "net 850.00" / "NET EUR 300.00"
        for line in lines:
            if re.match(r"^\s*net\b", line, re.I) and _last_amount(line) is not None:
                data["stated_net"] = _last_amount(line)
                break
    if not data["lines"] and len(singles) == 1 and data["stated_net"] is not None:
        data["lines"] = [{"vat_rate": singles[0][0], "net_amount": data["stated_net"], "stated_vat_amount": singles[0][1]}]
        notes.append("Net amount for the VAT line taken from the 'net' line - please verify")
    if data["stated_gross"] is None:
        data["stated_gross"] = _find_total(lines, ("total",), exclude=("net", "vat", "tax"))
        if data["stated_gross"] is not None:
            notes.append("Invoice total taken from a line labelled just 'Total' - please verify")
    return data


def extract_invoice(image_path):
    """Full pipeline for one image: OCR, parse, add file details."""
    text = ocr_image(image_path)
    data = parse_invoice_text(text)
    data["raw_text"] = text
    data["source_file"] = image_path
    data["file_hash"] = file_sha256(image_path)
    return data
