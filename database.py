import datetime
import decimal
import queue
import sys

import pymysql
import pymysql.cursors
from flask import g, has_app_context

import config

T = 'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'


# ── Rows behave like a dict AND support row[0] ─────────
class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _clean(value):
    """Convert MySQL types into plain Python values the templates expect."""
    if isinstance(value, decimal.Decimal):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, datetime.date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, datetime.timedelta):
        t = int(value.total_seconds())
        return f'{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}'
    return value


class Result:
    def __init__(self, cur):
        self._cur = cur

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        r = self._cur.fetchone()
        return Row((k, _clean(v)) for k, v in r.items()) if r else None

    def fetchall(self):
        return [Row((k, _clean(v)) for k, v in r.items())
                for r in self._cur.fetchall()]


# ── Connection pool (much faster than reconnecting every request) ──
_pool = queue.Queue(maxsize=10)

def _tz_offset():
    """The database clock always runs on Tanzania time."""
    return '+03:00'

def _connect(with_db=True):
    return pymysql.connect(
        host=config.MYSQL_HOST, port=config.MYSQL_PORT,
        user=config.MYSQL_USER, password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE if with_db else None,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        init_command=("SET SESSION sql_mode=REPLACE(@@sql_mode,'ONLY_FULL_GROUP_BY',''), "
                      f"SESSION time_zone='{_tz_offset()}'"),
        autocommit=False)


def _acquire():
    try:
        raw = _pool.get_nowait()
    except queue.Empty:
        return _connect()
    try:
        raw.ping(reconnect=True)
        return raw
    except Exception:
        return _connect()


def _release(raw):
    try:
        raw.rollback()          # never hand back a half-finished transaction
        _pool.put_nowait(raw)
    except Exception:           # pool full or connection broken
        try:
            raw.close()
        except Exception:
            pass


class Connection:
    """Small wrapper so queries can use ? placeholders."""
    def __init__(self, raw):
        self._raw = raw
        self._closed = False

    def execute(self, sql, params=None):
        if params:
            sql = sql.replace('%', '%%').replace('?', '%s')
            params = tuple(params)
        else:
            params = None
        cur = self._raw.cursor()
        cur.execute(sql, params)
        return Result(cur)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if not self._closed:
            self._closed = True
            _release(self._raw)


def get_db():
    conn = Connection(_acquire())
    if has_app_context():                      # make sure it is always returned
        if not hasattr(g, '_db_conns'):
            g._db_conns = []
        g._db_conns.append(conn)
    return conn


def release_request_connections(exc=None):
    for c in getattr(g, '_db_conns', []):
        c.close()
    g._db_conns = []


SCHEMA = [
f"""CREATE TABLE IF NOT EXISTS branches (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    location VARCHAR(200),
    phone VARCHAR(40),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) {T}""",

f"""CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    full_name VARCHAR(120) NOT NULL,
    username VARCHAR(60) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    `role` VARCHAR(20) DEFAULT 'cashier',
    branch_id INT NULL,
    active TINYINT(1) DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_users_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL
) {T}""",

# One row per login. Powers "active users" and automatic attendance.
f"""CREATE TABLE IF NOT EXISTS user_sessions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    branch_id INT NULL,
    login_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    logout_at DATETIME NULL,
    end_reason VARCHAR(10) NULL,
    KEY idx_sess_user_login (user_id, login_at),
    KEY idx_sess_login (login_at),
    KEY idx_sess_active (logout_at, last_seen),
    CONSTRAINT fk_sess_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) {T}""",

f"""CREATE TABLE IF NOT EXISTS login_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    username VARCHAR(60),
    action VARCHAR(20),
    `timestamp` DATETIME DEFAULT CURRENT_TIMESTAMP
) {T}""",

f"""CREATE TABLE IF NOT EXISTS departments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    description VARCHAR(255),
    branch_id INT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_dept_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS categories (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    department_id INT NULL,
    description VARCHAR(255),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_cat_dept FOREIGN KEY (department_id)
        REFERENCES departments(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    barcode VARCHAR(80) NULL UNIQUE,
    category_id INT NULL,
    branch_id INT NULL,
    buying_price DECIMAL(15,2) DEFAULT 0,
    selling_price DECIMAL(15,2) DEFAULT 0,
    stock_qty INT DEFAULT 0,
    min_stock INT DEFAULT 5,
    unit VARCHAR(20) DEFAULT 'pcs',
    description TEXT NULL,
    active TINYINT(1) DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_prod_cat FOREIGN KEY (category_id)
        REFERENCES categories(id) ON DELETE SET NULL,
    CONSTRAINT fk_prod_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS customers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    full_name VARCHAR(120) NOT NULL,
    phone VARCHAR(40),
    email VARCHAR(120),
    address VARCHAR(255),
    debit_limit DECIMAL(15,2) DEFAULT 0,
    debit_balance DECIMAL(15,2) DEFAULT 0,
    branch_id INT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_cust_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS sales (
    id INT AUTO_INCREMENT PRIMARY KEY,
    receipt_no VARCHAR(40) NOT NULL UNIQUE,
    branch_id INT NULL,
    user_id INT NULL,
    customer_id INT NULL,
    total_amount DECIMAL(15,2) DEFAULT 0,
    paid_amount DECIMAL(15,2) DEFAULT 0,
    change_amount DECIMAL(15,2) DEFAULT 0,
    payment_type VARCHAR(20) DEFAULT 'cash',
    status VARCHAR(20) DEFAULT 'completed',
    notes TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_sales_created (created_at),
    CONSTRAINT fk_sales_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL,
    CONSTRAINT fk_sales_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_sales_cust FOREIGN KEY (customer_id)
        REFERENCES customers(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS sale_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    sale_id INT NOT NULL,
    product_id INT NULL,
    quantity INT,
    unit_price DECIMAL(15,2),
    total_price DECIMAL(15,2),
    CONSTRAINT fk_items_sale FOREIGN KEY (sale_id)
        REFERENCES sales(id) ON DELETE CASCADE,
    CONSTRAINT fk_items_prod FOREIGN KEY (product_id)
        REFERENCES products(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS expenses (
    id INT AUTO_INCREMENT PRIMARY KEY,
    branch_id INT NULL,
    category VARCHAR(60),
    description VARCHAR(255),
    amount DECIMAL(15,2),
    recorded_by INT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_exp_created (created_at),
    CONSTRAINT fk_exp_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL,
    CONSTRAINT fk_exp_user FOREIGN KEY (recorded_by)
        REFERENCES users(id) ON DELETE SET NULL
) {T}""",

# Kept for old data. Attendance is now built from user_sessions.
f"""CREATE TABLE IF NOT EXISTS attendance (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    branch_id INT NULL,
    `date` DATE NOT NULL,
    check_in TIME NULL,
    check_out TIME NULL,
    status VARCHAR(20) DEFAULT 'present',
    UNIQUE KEY uq_attendance (user_id, `date`),
    CONSTRAINT fk_att_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_att_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS salaries (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    branch_id INT NULL,
    amount DECIMAL(15,2),
    `month` VARCHAR(7) NOT NULL,
    paid TINYINT(1) DEFAULT 0,
    paid_at DATETIME NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_salary (user_id, `month`),
    CONSTRAINT fk_sal_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_sal_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL
) {T}""",

f"""CREATE TABLE IF NOT EXISTS stock_transfers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NULL,
    from_branch INT NULL,
    to_branch INT NULL,
    quantity INT,
    transferred_by INT NULL,
    notes TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) {T}""",

f"""CREATE TABLE IF NOT EXISTS mobile_money_transactions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    branch_id INT NULL,
    provider VARCHAR(40) NOT NULL,
    `type` VARCHAR(10) NOT NULL,
    phone_number VARCHAR(40),
    amount DECIMAL(15,2) NOT NULL,
    reference VARCHAR(80),
    description VARCHAR(255),
    recorded_by INT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_mm_created (created_at),
    CONSTRAINT fk_mm_branch FOREIGN KEY (branch_id)
        REFERENCES branches(id) ON DELETE SET NULL,
    CONSTRAINT fk_mm_user FOREIGN KEY (recorded_by)
        REFERENCES users(id) ON DELETE SET NULL
) {T}""",
]


def _ensure_index(conn, table, name, columns):
    exists = conn.execute(
        'SELECT COUNT(*) FROM information_schema.statistics '
        'WHERE table_schema=DATABASE() AND table_name=? AND index_name=?',
        (table, name)).fetchone()[0]
    if not exists:
        conn.execute(f'ALTER TABLE `{table}` ADD INDEX `{name}` ({columns})')


def init_db():
    try:
        raw = _connect(with_db=False)
        raw.cursor().execute(
            f"CREATE DATABASE IF NOT EXISTS `{config.MYSQL_DATABASE}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        raw.commit()
        raw.close()
        conn = get_db()
    except pymysql.err.OperationalError as e:
        print("\nCould not connect to MySQL:", e)
        print("Check that MySQL is running and that config.py has the "
              "right user, password and port.\n")
        sys.exit(1)

    for ddl in SCHEMA:
        conn.execute(ddl)
    conn.commit()

    # Speed: indexes for the branch + date filters used by every report
    _ensure_index(conn, 'sales', 'idx_sales_branch_created', 'branch_id, created_at')
    _ensure_index(conn, 'expenses', 'idx_exp_branch_created', 'branch_id, created_at')
    _ensure_index(conn, 'mobile_money_transactions', 'idx_mm_branch_created',
                  'branch_id, created_at')
    _ensure_index(conn, 'products', 'idx_prod_active_branch', 'active, branch_id')
    conn.commit()

    if conn.execute('SELECT COUNT(*) FROM branches').fetchone()[0] == 0:
        conn.execute('INSERT INTO branches (name, location, phone) VALUES (?,?,?)',
                     ('Main Branch', 'Dar es Salaam', '+255700000000'))
        print("  Default branch created: Main Branch")

    if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        from auth import hash_password
        branch_id = conn.execute(
            'SELECT id FROM branches ORDER BY id LIMIT 1').fetchone()['id']
        conn.execute(
            'INSERT INTO users (full_name, username, password, `role`, branch_id) '
            'VALUES (?,?,?,?,?)',
            ('System Admin', 'admin', hash_password('admin123'), 'admin', branch_id))
        print("  Default user created: admin / admin123")

    conn.commit()
    conn.close()
    print(f"  MySQL database ready: {config.MYSQL_DATABASE}")