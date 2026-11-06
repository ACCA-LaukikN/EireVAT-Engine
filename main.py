"""
main.py
Entry point for the Irish VAT3 Compliance Tool.

Run with: python main.py

Provides an interactive CLI menu for:
- Adding single-rate transactions and multi-rate invoices
- Viewing VAT3 calculations for a period
- Generating the VAT3 return and marking a period as filed
- Checking overdue filing periods
- Creating new VAT periods
- Importing an invoice from a photo (OCR -> Excel review -> database)
"""

from db_setup import initialise_database
from vat_calculator import add_transaction, add_invoice, calculate_vat3, generate_vat3_report
from period_manager import generate_vat_period, mark_period_filed, check_overdue_periods
from import_invoice import stage_invoice, commit_invoice

RATE_HELP = """
VAT Rates:
  1 = Standard (23%)        2 = Reduced (13.5%)
  3 = Second Reduced (9%)   4 = Livestock (4.8%)
  5 = Zero Rated (0%)       6 = Exempt (0%)"""


def ask_optional_float(prompt):
    """Returns a float, or None if the user just presses Enter."""
    value = input(prompt).strip()
    return float(value) if value else None


def collect_header():
    """Prompts for the fields common to a transaction or an invoice."""
    business_id = int(input("Business ID: "))
    period_id = int(input("Period ID: "))
    date = input("Invoice date (YYYY-MM-DD): ").strip()
    trans_type = input("Type (sale/purchase): ").strip().lower()
    counterparty = input("Counterparty name: ")
    invoice_number = input("Invoice number: ")
    supply_type = input("Supply type (domestic/intra_eu/export): ").strip().lower()
    is_reverse_charge = input("Reverse charge? (y/n): ").strip().lower() == "y"
    counterparty_vat_no = None
    if supply_type == "intra_eu":
        counterparty_vat_no = input("Counterparty VAT number: ")
    return (business_id, period_id, date, trans_type, counterparty, invoice_number,
            supply_type, is_reverse_charge, counterparty_vat_no)


def main_menu(conn, cursor):
    while True:
        print("\n" + "=" * 44)
        print("  IRISH VAT3 COMPLIANCE TOOL")
        print("=" * 44)
        print("  1. Add a transaction (single VAT rate)")
        print("  2. Add an invoice with several VAT rates")
        print("  3. View VAT3 calculation for a period")
        print("  4. Generate full VAT3 report")
        print("  5. Mark a period as filed")
        print("  6. Check overdue periods")
        print("  7. Create new VAT period")
        print("  8. Import an invoice from a photo")
        print("  9. Exit")
        print("=" * 44)

        choice = input("Choose an option (1-9): ").strip()

        try:
            if choice == "1":
                print("\n--- ADD TRANSACTION ---")
                (business_id, period_id, date, trans_type, counterparty, invoice_number,
                 supply_type, is_reverse_charge, counterparty_vat_no) = collect_header()
                net_amount = float(input("Net amount (€): "))
                print(RATE_HELP)
                vat_rate_id = int(input("VAT rate ID: "))
                stated_vat = ask_optional_float("VAT shown on the invoice (€, Enter to skip): ")

                add_transaction(cursor, conn, business_id, period_id, date, trans_type,
                                counterparty, net_amount, vat_rate_id, invoice_number,
                                supply_type, counterparty_vat_no=counterparty_vat_no,
                                is_reverse_charge=is_reverse_charge, stated_vat_amount=stated_vat)

            elif choice == "2":
                print("\n--- ADD INVOICE (SEVERAL VAT RATES) ---")
                (business_id, period_id, date, trans_type, counterparty, invoice_number,
                 supply_type, is_reverse_charge, counterparty_vat_no) = collect_header()
                print(RATE_HELP)
                lines = []
                while True:
                    net = input(f"Net amount for line {len(lines) + 1} (€, Enter when finished): ").strip()
                    if not net:
                        break
                    rate_id = int(input("  VAT rate ID: "))
                    stated = ask_optional_float("  VAT shown on invoice for this rate (€, Enter to skip): ")
                    lines.append({"net_amount": float(net), "vat_rate_id": rate_id,
                                  "stated_vat_amount": stated})
                stated_gross = ask_optional_float("Invoice total shown on document (€, Enter to skip): ")

                add_invoice(cursor, conn, business_id, period_id, date, trans_type, counterparty,
                            invoice_number, supply_type, lines,
                            counterparty_vat_no=counterparty_vat_no,
                            is_reverse_charge=is_reverse_charge, stated_gross=stated_gross)

            elif choice == "3":
                print("\n--- VAT3 CALCULATION ---")
                period_id = int(input("Period ID: "))
                result = calculate_vat3(cursor, period_id)
                print()
                for box, value in result.items():
                    print(f"  {box}: €{value:.2f}")

            elif choice == "4":
                print("\n--- GENERATE VAT3 REPORT ---")
                business_id = int(input("Business ID: "))
                period_id = int(input("Period ID: "))
                generate_vat3_report(cursor, conn, period_id, business_id)
                if input("Mark this period as filed? (y/n): ").strip().lower() == "y":
                    mark_period_filed(cursor, conn, period_id)

            elif choice == "5":
                print("\n--- MARK PERIOD AS FILED ---")
                period_id = int(input("Period ID: "))
                mark_period_filed(cursor, conn, period_id)

            elif choice == "6":
                print("\n--- OVERDUE PERIODS ---")
                business_id = int(input("Business ID: "))
                check_overdue_periods(cursor, business_id)

            elif choice == "7":
                print("\n--- CREATE VAT PERIOD ---")
                business_id = int(input("Business ID: "))
                period_start = input("Period start date (YYYY-MM-DD, must be 1st of month): ")
                generate_vat_period(cursor, conn, business_id, period_start)

            elif choice == "8":
                print("\n--- IMPORT INVOICE FROM PHOTO ---")
                business_id = int(input("Business ID: "))
                period_id = int(input("Period ID: "))
                image_path = input("Path to the invoice image: ").strip().strip('"')
                trans_type = input("Type (sale/purchase) [purchase]: ").strip().lower() or "purchase"
                supply_type = input("Supply type (domestic/intra_eu/export) [domestic]: ").strip().lower() or "domestic"
                review_path, _, _ = stage_invoice(cursor, image_path, business_id, period_id,
                                                  trans_type=trans_type, supply_type=supply_type)
                input(f"\nOpen {review_path}, compare it with the invoice, correct and SAVE it, then press Enter here...")
                commit_invoice(cursor, conn, business_id, period_id, review_path)

            elif choice == "9":
                print("\nExiting. All data saved to vat3.db")
                break

            else:
                print("Invalid option — please enter a number between 1 and 9")

        except ValueError as error:
            # Bad number typed, or a validation rule in the business logic rejected the input
            print(f"\nNot saved — {error}")


if __name__ == "__main__":
    conn, cursor = initialise_database()
    main_menu(conn, cursor)
