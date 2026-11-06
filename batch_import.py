"""
batch_import.py
Import MANY invoice images at once - still with a human review step.

  1. stage_folder():  reads every .jpg/.jpeg/.png in a folder, writes one review workbook per invoice
                      into a review folder, and prints a summary table
  2. (you)            open the workbooks flagged with errors or warnings, correct them, save
  3. commit_folder(): saves every workbook that passes the checks; reports the ones it skipped

Tip: put purchase invoices and sales invoices in separate folders and run each with the right trans_type.
"""

import csv
import os
import shutil

from import_invoice import stage_invoice, commit_invoice
from invoice_validator import has_errors

IMAGE_TYPES = (".jpg", ".jpeg", ".png")


def stage_folder(cursor, folder, business_id, review_dir="reviews", trans_type="purchase", supply_type="domestic"):
    os.makedirs(review_dir, exist_ok=True)
    names = sorted(n for n in os.listdir(folder) if n.lower().endswith(IMAGE_TYPES))
    if not names:
        print(f"No .jpg/.jpeg/.png files found in {folder}")
        return []

    summary = []
    for name in names:
        review_path = os.path.join(review_dir, os.path.splitext(name)[0] + "_review.xlsx")
        try:
            _, data, issues = stage_invoice(cursor, os.path.join(folder, name), business_id, None,
                                            review_path, trans_type, supply_type, quiet=True)
            errors = sum(1 for i in issues if i["level"] == "ERROR")
            warnings = sum(1 for i in issues if i["level"] == "WARNING") + len(data.get("extraction_notes", []))
            row = {"file": name, "counterparty": data.get("supplier"), "invoice_no": data.get("invoice_number"),
                   "date": data.get("invoice_date"), "total": data.get("stated_gross"), "period": data.get("period_id"),
                   "errors": errors, "warnings": warnings,
                   "first_problem": next((i["message"] for i in issues if i["level"] == "ERROR"), "")}
        except Exception as error:                      # an unreadable file must not stop the rest of the batch
            row = {"file": name, "counterparty": None, "invoice_no": None, "date": None, "total": None,
                   "period": None, "errors": 1, "warnings": 0, "first_problem": f"Could not read file: {error}"}
        summary.append(row)

    with open(os.path.join(review_dir, "summary.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0].keys()))
        writer.writeheader()
        writer.writerows(summary)

    print(f"{'file':34} {'counterparty':28} {'invoice':14} {'date':11} {'total':>9} {'err':>3} {'warn':>4}")
    for r in summary:
        print(f"{r['file'][:34]:34} {str(r['counterparty'])[:28]:28} {str(r['invoice_no'])[:14]:14} "
              f"{str(r['date']):11} {str(r['total']):>9} {r['errors']:>3} {r['warnings']:>4}")
        if r["first_problem"]:
            print(f"    -> {r['first_problem']}")
    clean = sum(1 for r in summary if r["errors"] == 0)
    print(f"\n{len(summary)} invoice(s) read: {clean} without errors, {len(summary) - clean} need fixing. "
          f"Review workbooks are in '{review_dir}/'. Nothing has been saved to the database yet.")
    return summary


def commit_folder(cursor, conn, business_id, review_dir="reviews"):
    """Saves every review workbook that passes the checks. Safe to re-run: saved or duplicate invoices are skipped."""
    names = sorted(n for n in os.listdir(review_dir) if n.lower().endswith("_review.xlsx"))
    saved, skipped = 0, []
    for name in names:
        try:
            result = commit_invoice(cursor, conn, business_id, None, os.path.join(review_dir, name), quiet=True)
        except ValueError as error:
            result = None
            skipped.append((name, str(error)))
            continue
        if result is None:
            skipped.append((name, "failed a check or already imported - open the workbook's Issues tab"))
        else:
            saved += 1
    print(f"Saved {saved} invoice(s); skipped {len(skipped)}.")
    for name, reason in skipped:
        print(f"  skipped {name}: {reason}")
    return saved, skipped


def zip_reviews(review_dir="reviews", archive_name="reviews"):
    """Zips the review folder so you can download it in one go (Colab). Returns the zip file name."""
    return shutil.make_archive(archive_name, "zip", review_dir)
