"""
add_demo_business.py
One-off helper: adds a FICTIONAL business and its first VAT period to vat3.db
so you can try the menu in main.py. Safe to run more than once.

Run with: python add_demo_business.py
"""

from db_setup import initialise_database
from period_manager import generate_vat_period

conn, cursor = initialise_database(quiet=True)

cursor.execute("SELECT business_id FROM businesses WHERE business_name = 'Demo Traders Ltd'")
row = cursor.fetchone()

if row:
    business_id = row[0]
    print(f"Demo business already exists (Business ID {business_id})")
else:
    cursor.execute('''
        INSERT INTO businesses (business_name, vat_registration_no, vat_period_type, accounting_basis)
        VALUES ('Demo Traders Ltd', 'IE1234567T', 'bi-monthly', 'invoice')
    ''')
    conn.commit()
    business_id = cursor.lastrowid
    period_id = generate_vat_period(cursor, conn, business_id, "2026-11-01")
    print(f"Created Demo Traders Ltd (Business ID {business_id}) with Period ID {period_id}")

print("Use these IDs when main.py asks for Business ID and Period ID.")
