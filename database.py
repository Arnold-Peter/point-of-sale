import sqlite3
from datetime import datetime

DB = 'noddy_store.db'

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # Branches
    c.execute('''CREATE TABLE IF NOT EXISTS branches (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        location   TEXT,
        phone      TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    # Users
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name  TEXT NOT NULL,
        username   TEXT UNIQUE NOT NULL,
        password   TEXT NOT NULL,
        role       TEXT DEFAULT 'cashier',
        branch_id  INTEGER,
        active     INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_id) REFERENCES branches(id)
    )''')

    # Login logs
    c.execute('''CREATE TABLE IF NOT EXISTS login_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER,
        username   TEXT,
        action     TEXT,
        timestamp  TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    # Departments
    c.execute('''CREATE TABLE IF NOT EXISTS departments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        description TEXT,
        branch_id   INTEGER,
        created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_id) REFERENCES branches(id)
    )''')

    # Categories
    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        department_id INTEGER,
        description   TEXT,
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (department_id) REFERENCES departments(id)
    )''')

    # Products
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        barcode       TEXT UNIQUE,
        category_id   INTEGER,
        branch_id     INTEGER,
        buying_price  REAL DEFAULT 0,
        selling_price REAL DEFAULT 0,
        stock_qty     INTEGER DEFAULT 0,
        min_stock     INTEGER DEFAULT 5,
        unit          TEXT DEFAULT 'pcs',
        description   TEXT,
        active        INTEGER DEFAULT 1,
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (category_id) REFERENCES categories(id),
        FOREIGN KEY (branch_id)   REFERENCES branches(id)
    )''')

    # Sales
    c.execute('''CREATE TABLE IF NOT EXISTS sales (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_no   TEXT UNIQUE,
        branch_id    INTEGER,
        user_id      INTEGER,
        customer_id  INTEGER,
        total_amount REAL DEFAULT 0,
        paid_amount  REAL DEFAULT 0,
        change_amount REAL DEFAULT 0,
        payment_type TEXT DEFAULT 'cash',
        status       TEXT DEFAULT 'completed',
        notes        TEXT,
        created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_id)   REFERENCES branches(id),
        FOREIGN KEY (user_id)     REFERENCES users(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    )''')

    # Sale items
    c.execute('''CREATE TABLE IF NOT EXISTS sale_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id     INTEGER,
        product_id  INTEGER,
        quantity    INTEGER,
        unit_price  REAL,
        total_price REAL,
        FOREIGN KEY (sale_id)    REFERENCES sales(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')

    # Customers (including debit customers)
    c.execute('''CREATE TABLE IF NOT EXISTS customers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name    TEXT NOT NULL,
        phone        TEXT,
        email        TEXT,
        address      TEXT,
        debit_limit  REAL DEFAULT 0,
        debit_balance REAL DEFAULT 0,
        branch_id    INTEGER,
        created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_id) REFERENCES branches(id)
    )''')

    # Expenses
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_id   INTEGER,
        category    TEXT,
        description TEXT,
        amount      REAL,
        recorded_by INTEGER,
        created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_id)   REFERENCES branches(id),
        FOREIGN KEY (recorded_by) REFERENCES users(id)
    )''')

    # Staff attendance
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER,
        branch_id  INTEGER,
        date       TEXT,
        check_in   TEXT,
        check_out  TEXT,
        status     TEXT DEFAULT 'present',
        FOREIGN KEY (user_id)   REFERENCES users(id),
        FOREIGN KEY (branch_id) REFERENCES branches(id)
    )''')

    # Staff salaries
    c.execute('''CREATE TABLE IF NOT EXISTS salaries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER,
        branch_id   INTEGER,
        amount      REAL,
        month       TEXT,
        paid        INTEGER DEFAULT 0,
        paid_at     TEXT,
        created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id)   REFERENCES users(id),
        FOREIGN KEY (branch_id) REFERENCES branches(id)
    )''')

    # Stock transfers between branches
    c.execute('''CREATE TABLE IF NOT EXISTS stock_transfers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id   INTEGER,
        from_branch  INTEGER,
        to_branch    INTEGER,
        quantity     INTEGER,
        transferred_by INTEGER,
        notes        TEXT,
        created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (product_id)      REFERENCES products(id),
        FOREIGN KEY (from_branch)     REFERENCES branches(id),
        FOREIGN KEY (to_branch)       REFERENCES branches(id),
        FOREIGN KEY (transferred_by)  REFERENCES users(id)
    )''')
# Mobile money transactions
    c.execute('''CREATE TABLE IF NOT EXISTS mobile_money_transactions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_id    INTEGER,
        provider     TEXT NOT NULL,
        type         TEXT NOT NULL,
        phone_number TEXT,
        amount       REAL NOT NULL,
        reference    TEXT,
        description  TEXT,
        recorded_by  INTEGER,
        created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_id)   REFERENCES branches(id),
        FOREIGN KEY (recorded_by) REFERENCES users(id)
    )''')





    conn.commit()

    # ── Seed default data ──────────────────────────────
    # Default branch
    c.execute('SELECT COUNT(*) FROM branches')
    if c.fetchone()[0] == 0:
        c.execute('''INSERT INTO branches (name, location, phone)
                     VALUES (?,?,?)''',
                  ('Main Branch', 'Dar es Salaam', '+255700000000'))
        conn.commit()
        print("  Default branch created: Main Branch")

    # Default admin user
    from auth import hash_password
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        c.execute('''INSERT INTO users
                     (full_name, username, password, role, branch_id)
                     VALUES (?,?,?,?,?)''',
                  ('System Admin', 'admin',
                   hash_password('admin123'), 'admin', 1))
        conn.commit()
        print("  Default user created: admin / admin123")

    conn.close()
    print("  Database ready!")