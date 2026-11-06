"""
db_setup.py
Creates the SQLite database and all tables required for the Irish VAT3
compliance tool. Seeds the vat_rates table with Irish VAT rates.

Run this once to initialise vat3.db before using any other module.

Notes:
- New `invoices` table (one row per invoice; supports multi-rate invoices)
- `transactions` gains `invoice_id` and `stated_vat_amount`
- migrate_schema() upgrades an existing vat3.db in place (safe to run repeatedly)
- Unique indexes act as a database-level safety net against duplicate returns
  and duplicate invoice lines
- Foreign keys are now enforced (SQLite leaves them off by default)
"""

import sqlite3

DB_NAME = "vat3.db"


def get_connection(db_name=DB_NAME):
    """Returns a connection and cursor to the VAT3 database."""
    conn = sqlite3.connect(db_name)
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()
    return conn, cursor


def create_tables(cursor):
    """Creates all tables required by the project if they don't already exist."""

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vat_rates (
            rate_id INTEGER PRIMARY KEY,
            rate_name TEXT NOT NULL,
            rate_percentage DECIMAL NOT NULL,
            effective_from DATE NOT NULL,
            effective_to DATE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS businesses (
            business_id INTEGER PRIMARY KEY,
            business_name TEXT NOT NULL,
            vat_registration_no TEXT NOT NULL,
            vat_period_type TEXT NOT NULL,
            accounting_basis TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vat_periods (
            period_id INTEGER PRIMARY KEY,
            business_id INTEGER NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            due_date DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            FOREIGN KEY (business_id) REFERENCES businesses(business_id)
        )
    ''')

    # One row per invoice. A multi-rate invoice has ONE row here and
    # SEVERAL rows in `transactions` (one per VAT rate), linked by invoice_id.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id INTEGER PRIMARY KEY,
            business_id INTEGER NOT NULL,
            period_id INTEGER NOT NULL,
            counterparty_name TEXT NOT NULL COLLATE NOCASE,
            counterparty_vat_no TEXT,
            invoice_number TEXT NOT NULL,
            invoice_date DATE NOT NULL,
            gross_total DECIMAL,
            source_file TEXT,
            file_hash TEXT,
            status TEXT NOT NULL DEFAULT 'approved',
            extracted_json TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (business_id, counterparty_name, invoice_number),
            FOREIGN KEY (business_id) REFERENCES businesses(business_id),
            FOREIGN KEY (period_id) REFERENCES vat_periods(period_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id INTEGER PRIMARY KEY,
            business_id INTEGER NOT NULL,
            period_id INTEGER NOT NULL,
            transaction_date DATE NOT NULL,
            transaction_type TEXT NOT NULL,
            counterparty_name TEXT NOT NULL,
            counterparty_vat_no TEXT,
            net_amount DECIMAL NOT NULL,
            vat_rate_id INTEGER NOT NULL,
            vat_amount DECIMAL NOT NULL,
            gross_amount DECIMAL NOT NULL,
            invoice_number TEXT,
            supply_type TEXT NOT NULL,
            is_reverse_charge BOOLEAN DEFAULT 0,
            invoice_id INTEGER REFERENCES invoices(invoice_id),
            stated_vat_amount DECIMAL,
            FOREIGN KEY (business_id) REFERENCES businesses(business_id),
            FOREIGN KEY (period_id) REFERENCES vat_periods(period_id),
            FOREIGN KEY (vat_rate_id) REFERENCES vat_rates(rate_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vat3_returns (
            return_id INTEGER PRIMARY KEY,
            period_id INTEGER NOT NULL,
            T1_vat_on_sales DECIMAL,
            T2_vat_on_purchases DECIMAL,
            T3_vat_payable DECIMAL,
            T4_vat_repayable DECIMAL,
            E1_intra_eu_sales DECIMAL,
            E2_intra_eu_purchases DECIMAL,
            generated_at DATETIME,
            FOREIGN KEY (period_id) REFERENCES vat_periods(period_id)
        )
    ''')


def _column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate_schema(cursor):
    """
    Upgrades a database created by an earlier version of this tool.
    Safe to run on every start-up: each step checks before it changes anything.
    """

    # 1. New columns on transactions (CREATE TABLE IF NOT EXISTS won't add these to an old table)
    if not _column_exists(cursor, "transactions", "invoice_id"):
        cursor.execute("ALTER TABLE transactions ADD COLUMN invoice_id INTEGER REFERENCES invoices(invoice_id)")
    if not _column_exists(cursor, "transactions", "stated_vat_amount"):
        cursor.execute("ALTER TABLE transactions ADD COLUMN stated_vat_amount DECIMAL")

    # 2. Unique indexes. If an old database already holds duplicates, creating
    #    the index fails, so we warn instead of crashing.
    indexes = {
        "idx_vat3_returns_period":
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_vat3_returns_period "
            "ON vat3_returns(period_id)",
        "idx_transactions_invoice_line":
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_invoice_line "
            "ON transactions(business_id, counterparty_name COLLATE NOCASE, invoice_number, vat_rate_id) "
            "WHERE invoice_number IS NOT NULL",
        "idx_invoices_file_hash":
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_file_hash "
            "ON invoices(business_id, file_hash) WHERE file_hash IS NOT NULL",
    }
    for name, sql in indexes.items():
        try:
            cursor.execute(sql)
        except sqlite3.IntegrityError:
            print(f"WARNING: could not create index {name} - existing duplicate rows must be cleaned first")


def seed_vat_rates(cursor):
    """Seeds the vat_rates table with Irish VAT rates. Check these against Revenue.ie before relying on them."""

    irish_vat_rates = [
        (1, "Standard", 23.0, "2012-01-01", None),
        (2, "Reduced", 13.5, "1985-01-01", None),
        (3, "Second Reduced", 9.0, "2011-07-01", None),
        (4, "Livestock", 4.8, "1985-01-01", None),
        (5, "Zero Rated", 0.0, "1972-11-01", None),
        (6, "Exempt", 0.0, "1972-11-01", None),
    ]

    cursor.executemany('''
        INSERT OR IGNORE INTO vat_rates 
        (rate_id, rate_name, rate_percentage, effective_from, effective_to)
        VALUES (?, ?, ?, ?, ?)
    ''', irish_vat_rates)


def initialise_database(db_name=DB_NAME, quiet=False):
    """Main entry point - creates, upgrades and seeds the database in one call."""
    conn, cursor = get_connection(db_name)
    create_tables(cursor)
    migrate_schema(cursor)
    seed_vat_rates(cursor)
    conn.commit()
    if not quiet:
        print(f"Database initialised successfully — {db_name} ready")
    return conn, cursor


if __name__ == "__main__":
    initialise_database()
