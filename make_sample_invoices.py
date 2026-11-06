"""
make_sample_invoices.py
Creates FICTIONAL sample invoice images in ./sample_invoices for testing the photo import.
No real supplier, customer or VAT number is used.

Run with: python make_sample_invoices.py
"""
import os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "DejaVuSans.ttf", "arial.ttf",
]
BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf", "arialbd.ttf",
]


def load_font(candidates, size):
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_invoice(path, supplier, address, vat_no, inv_no, date_text, bill_to, items, summary, totals,
                 photo_style=False):
    W, H = 1240, 1500
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f, fb, fh = load_font(FONT_CANDIDATES, 30), load_font(BOLD_CANDIDATES, 30), load_font(BOLD_CANDIDATES, 52)

    y = 70
    d.text((80, y), supplier, font=fh, fill="black"); y += 80
    for line in address:
        d.text((80, y), line, font=f, fill="black"); y += 42
    d.text((80, y), f"VAT No: {vat_no}", font=f, fill="black"); y += 90

    d.text((80, y), "INVOICE", font=fh, fill="black"); y += 80
    d.text((80, y), f"Invoice No: {inv_no}", font=f, fill="black"); y += 42
    d.text((80, y), f"Date: {date_text}", font=f, fill="black"); y += 42
    d.text((80, y), f"Bill to: {bill_to}", font=f, fill="black"); y += 90

    d.text((80, y), "Description", font=fb, fill="black")
    d.text((700, y), "Qty", font=fb, fill="black")
    d.text((880, y), "Net EUR", font=fb, fill="black")
    d.text((1060, y), "VAT %", font=fb, fill="black"); y += 46
    d.line((80, y, 1160, y), fill="black", width=2); y += 14
    for desc, qty, net, rate in items:
        d.text((80, y), desc, font=f, fill="black")
        d.text((700, y), str(qty), font=f, fill="black")
        d.text((880, y), f"{net:.2f}", font=f, fill="black")
        d.text((1060, y), f"{rate}%", font=f, fill="black"); y += 46
    y += 40

    d.text((80, y), "VAT Summary", font=fb, fill="black"); y += 46
    d.text((80, y), "Rate", font=fb, fill="black")
    d.text((420, y), "Net", font=fb, fill="black")
    d.text((760, y), "VAT", font=fb, fill="black"); y += 46
    for rate, net, vat in summary:
        d.text((80, y), f"{rate}%", font=f, fill="black")
        d.text((420, y), f"{net:.2f}", font=f, fill="black")
        d.text((760, y), f"{vat:.2f}", font=f, fill="black"); y += 46
    y += 40
    net_total, vat_total, gross = totals
    d.text((80, y), f"Total Net: EUR {net_total:.2f}", font=f, fill="black"); y += 46
    d.text((80, y), f"Total VAT: EUR {vat_total:.2f}", font=f, fill="black"); y += 46
    d.text((80, y), f"Total Due: EUR {gross:.2f}", font=fb, fill="black"); y += 80
    d.text((80, y), "Payment terms: 30 days. Thank you for your business.", font=f, fill="black")

    if photo_style:  # slight tilt, blur, grey cast and noise to mimic a phone photo
        random.seed(5)
        img = img.rotate(2.2, expand=True, fillcolor=(235, 235, 230))
        img = img.filter(ImageFilter.GaussianBlur(1.1))
        px = img.load()
        for _ in range(60000):
            x, yy = random.randrange(img.width), random.randrange(img.height)
            r, g, b = px[x, yy]
            n = random.randint(-25, 10)
            px[x, yy] = (max(0, r + n), max(0, g + n), max(0, b + n))
    img.save(path, quality=92)
    print("Created", path)


if __name__ == "__main__":
    os.makedirs("sample_invoices", exist_ok=True)

    # 1. Clean single-rate invoice (all 23%)
    draw_invoice(
        "sample_invoices/invoice_single_rate.png",
        "Lakeside Office Supplies Ltd", ["12 Harbour Road", "Galway, H91 AB12", "Ireland"],
        "IE9876543T", "INV-2026-0451", "02/11/2026", "Demo Traders Ltd",
        [("A4 printer paper (10 reams)", 10, 45.00, 23), ("Lever arch folders", 12, 30.00, 23),
         ("Desk organiser", 2, 25.00, 23)],
        [(23, 100.00, 23.00)], (100.00, 23.00, 123.00))

    # 2. Multi-rate invoice (23% and 0%), photographed
    draw_invoice(
        "sample_invoices/invoice_multi_rate_photo.png",
        "Corrib Training Resources Ltd", ["Unit 4, Docks Business Park", "Galway, H91 XY99", "Ireland"],
        "IE1357924W", "CTR/7788", "01 November 2026", "Demo Traders Ltd",
        [("Laminator", 1, 100.00, 23), ("Printed training manuals", 5, 50.00, 0)],
        [(23, 100.00, 23.00), (0, 50.00, 0.00)], (150.00, 23.00, 173.00), photo_style=True)
