import io
import random
import string
import time
import datetime as dt
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash, send_file)

from database import init_db, get_db, release_request_connections
from auth import verify_user, log_action, hash_password

app = Flask(__name__)
app.secret_key = 'noddystore_secret_2024'
app.jinja_env.filters['enumerate'] = enumerate
app.jinja_env.globals['enumerate'] = enumerate
def clock(value):
    """'2026-11-25 14:05:09' -> '2:05:09 PM'"""
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').strftime('%I:%M:%S %p').lstrip('0')
    except (TypeError, ValueError):
        return '—'


app.jinja_env.filters['clock'] = clock
app.teardown_appcontext(release_request_connections)

# ── Activity settings ──────────────────────────────────
IDLE_SECONDS = 300            # 5 minutes without interaction = inactive (keep same as activity.js)
TOUCH_EVERY = 30              # seconds between "last seen" writes to MySQL
PASSIVE_ENDPOINTS = {'api_online'}   # background polling never counts as activity
ACTIVE_SQL = (f"(s.logout_at IS NULL AND s.last_seen >= "
              f"DATE_SUB(NOW(), INTERVAL {IDLE_SECONDS} SECOND))")


# ── Helpers ────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('Only the admin can do that.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def bf(column):
    """Branch filter: admin sees every branch, others only their own."""
    if session.get('role') == 'admin':
        return ''
    return f"AND {column}={int(session.get('branch_id') or 0)}"


def dr(col):
    """Date-range filter that can use an index. Params: (date_from, date_to)."""
    return f"{col} >= ? AND {col} < DATE_ADD(?, INTERVAL 1 DAY)"


def mr(col):
    """Month filter that can use an index. Params: (first_day, first_day)."""
    return f"{col} >= ? AND {col} < DATE_ADD(?, INTERVAL 1 MONTH)"


def generate_receipt_no():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f'RCP-{datetime.now().strftime("%Y%m%d")}-{rand}'


def pnl(conn, date_from, date_to):
    """Revenue, cost, gross and net profit for a date range."""
    bs, be = bf('s.branch_id'), bf('branch_id')
    sr = conn.execute(f'''
        SELECT COALESCE(SUM(s.total_amount),0) AS revenue, COUNT(*) AS transactions
        FROM sales s
        WHERE {dr('s.created_at')} {bs}
    ''', (date_from, date_to)).fetchone()
    cost = conn.execute(f'''
        SELECT COALESCE(SUM(si.quantity*p.buying_price),0)
        FROM sale_items si
        JOIN sales s ON si.sale_id=s.id
        JOIN products p ON si.product_id=p.id
        WHERE {dr('s.created_at')} {bs}
    ''', (date_from, date_to)).fetchone()[0]
    expenses = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0) FROM expenses
        WHERE {dr('created_at')} {be}
    ''', (date_from, date_to)).fetchone()[0]
    revenue = sr['revenue']
    gross = revenue - cost
    return {
        'revenue': revenue, 'transactions': sr['transactions'],
        'cost': cost, 'gross_profit': gross, 'expenses': expenses,
        'net_profit': gross - expenses,
        'margin': (gross / revenue * 100) if revenue > 0 else 0,
    }


# ── Session tracking (active users + attendance) ───────
def start_session(user):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO user_sessions (user_id, branch_id) VALUES (?,?)',
        (user['id'], user['branch_id']))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def end_session(sid, reason):
    """reason='logout' ends now; reason='timeout' ends at the last activity."""
    if not sid:
        return
    conn = get_db()
    if reason == 'timeout':
        conn.execute("UPDATE user_sessions SET logout_at=last_seen, end_reason='timeout' "
                     "WHERE id=? AND logout_at IS NULL", (sid,))
    else:
        conn.execute("UPDATE user_sessions SET logout_at=NOW(), last_seen=NOW(), "
                     "end_reason='logout' WHERE id=? AND logout_at IS NULL", (sid,))
    conn.commit()
    conn.close()


def touch_session(sid, user_id):
    """Refresh last_seen. Returns False if the account was disabled or deleted."""
    conn = get_db()
    if sid:
        conn.execute('UPDATE user_sessions SET last_seen=NOW() '
                     'WHERE id=? AND logout_at IS NULL', (sid,))
    row = conn.execute('SELECT active FROM users WHERE id=?', (user_id,)).fetchone()
    conn.commit()
    conn.close()
    return bool(row and row['active'])


def online_users(conn, scope=''):
    return conn.execute(f'''
        SELECT u.id, u.full_name, u.`role`, b.name AS branch_name,
               MIN(s.login_at) AS since, MAX(s.last_seen) AS last_seen
        FROM user_sessions s
        JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON u.branch_id=b.id
        WHERE s.logout_at IS NULL
          AND s.last_seen >= DATE_SUB(NOW(), INTERVAL {IDLE_SECONDS} SECOND) {scope}
        GROUP BY u.id, u.full_name, u.`role`, b.name
        ORDER BY u.full_name
    ''').fetchall()


def force_logout(message, reason):
    end_session(session.get('sid'), reason)
    session.clear()
    session['login_notice'] = message
    if request.path.startswith('/api/') or request.path == '/pos/checkout':
        return jsonify({'success': False, 'expired': True, 'message': message}), 401
    return redirect(url_for('login'))


@app.before_request
def track_activity():
    if request.endpoint in (None, 'static', 'login') or 'user_id' not in session:
        return
    now = time.time()
    last = session.get('last_active', now)
    if now - last > IDLE_SECONDS:
        return force_logout('You were signed out after 5 minutes of inactivity. '
                            'Please sign in again.', 'timeout')
    if request.endpoint in PASSIVE_ENDPOINTS:
        return
    if now - last > 5:
        session['last_active'] = now
    if now - session.get('last_touch', 0) > TOUCH_EVERY:
        session['last_touch'] = now
        try:
            if not touch_session(session.get('sid'), session['user_id']):
                return force_logout('Your account is no longer active.', 'logout')
        except Exception:
            pass          # tracking must never break a page


# ── Auth ───────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    error = session.pop('login_notice', None)
    if request.method == 'POST':
        user = verify_user(request.form.get('username', '').strip(),
                           request.form.get('password', ''))
        if user:
            sid = start_session(user)
            session.clear()
            now = time.time()
            session.update({
                'user_id':     user['id'],
                'username':    user['username'],
                'full_name':   user['full_name'],
                'role':        user['role'],
                'branch_id':   user['branch_id'],
                'branch_name': user['branch_name'] or 'All Branches',
                'sid':         sid,
                'last_active': now,
                'last_touch':  now,
            })
            log_action(user['id'], user['username'], 'login')
            return redirect(url_for('dashboard'))
        error = 'Invalid username or password. Please try again.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    if 'user_id' in session:
        idle = request.args.get('reason') == 'idle'
        end_session(session.get('sid'), 'timeout' if idle else 'logout')
        log_action(session['user_id'], session['username'], 'logout')
        session.clear()
        if idle:
            session['login_notice'] = ('You were signed out after 5 minutes of '
                                       'inactivity. Please sign in again.')
    return redirect(url_for('login'))


@app.route('/api/heartbeat', methods=['POST'])
@login_required
def api_heartbeat():
    # track_activity() has already refreshed the session
    return jsonify({'ok': True})



@app.route('/api/online')
@login_required
def api_online():
    if session.get('role') != 'admin':
        return jsonify({'error': 'forbidden'}), 403
    conn = get_db()
    rows = online_users(conn)
    conn.close()
    return jsonify({'count': len(rows), 'users': [
        {'id': r['id'], 'full_name': r['full_name'], 'role': r['role'],
         'branch_name': r['branch_name'] or '—',
         'since': clock(r['since']), 'last_seen': clock(r['last_seen'])} for r in rows]})


@app.route('/api/leave', methods=['POST'])
def api_leave():
    """Sent by the browser when a tab is closed, so 'last seen' is exact."""
    sid = session.get('sid')
    if sid:
        conn = get_db()
        conn.execute('UPDATE user_sessions SET last_seen=NOW() '
                     'WHERE id=? AND logout_at IS NULL', (sid,))
        conn.commit()
        conn.close()
    return ('', 204)
# ── Dashboard ──────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    conn = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    bs, be, bp = bf('s.branch_id'), bf('branch_id'), bf('p.branch_id')
    pl = pnl(conn, today, today)

    total_products = conn.execute(
        f"SELECT COUNT(*) FROM products p WHERE p.active=1 {bp}").fetchone()[0]
    total_customers = conn.execute(
        f"SELECT COUNT(*) FROM customers WHERE 1=1 {be}").fetchone()[0]
    total_staff = conn.execute(
        f"SELECT COUNT(*) FROM users WHERE active=1 AND role<>'admin' {be}").fetchone()[0]
    debit_customers = conn.execute(
        f"SELECT COUNT(*) FROM customers WHERE debit_balance>0 {be}").fetchone()[0]
    low_stock_count = conn.execute(
        f"SELECT COUNT(*) FROM products p "
        f"WHERE p.active=1 AND p.stock_qty<=p.min_stock {bp}").fetchone()[0]

    low_stock_items = conn.execute(f'''
        SELECT p.name, p.stock_qty, p.min_stock, p.unit,
               c.name AS cat_name, b.name AS branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.active=1 AND p.stock_qty<=p.min_stock {bp}
        ORDER BY p.stock_qty ASC LIMIT 12
    ''').fetchall()

    if session.get('role') == 'admin':
        branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    else:
        branches = conn.execute('SELECT * FROM branches WHERE id=?',
                                (session.get('branch_id') or 0,)).fetchall()

    recent_sales = conn.execute(f'''
        SELECT s.*, u.full_name AS cashier, b.name AS branch_name
        FROM sales s
        LEFT JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON s.branch_id=b.id
        WHERE 1=1 {bs}
        ORDER BY s.created_at DESC LIMIT 8
    ''').fetchall()

    mm_today = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS `count`
        FROM mobile_money_transactions
        WHERE {dr('created_at')} AND `type`='credit' {be}
    ''', (today, today)).fetchone()

    conn.close()
    return render_template('dashboard.html',
        revenue_today=pl['revenue'], cost_today=pl['cost'],
        gross_today=pl['gross_profit'], net_today=pl['net_profit'],
        expenses_today=pl['expenses'], txns_today=pl['transactions'],
        total_products=total_products, total_customers=total_customers,
        total_staff=total_staff, debit_customers=debit_customers,
        low_stock_items=low_stock_items, low_stock_count=low_stock_count,
        branches=branches, recent_sales=recent_sales,
        mm_today=mm_today, today=today)


# ── Branches ───────────────────────────────────────────
@app.route('/branches')
@login_required
def branches():
    conn = get_db()
    rows = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('branches.html', branches=rows)


@app.route('/branches/add', methods=['POST'])
@admin_required
def add_branch():
    conn = get_db()
    conn.execute('INSERT INTO branches (name, location, phone) VALUES (?,?,?)',
                 (request.form['name'], request.form.get('location', ''),
                  request.form.get('phone', '')))
    conn.commit()
    conn.close()
    flash('Branch added!', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/delete/<int:bid>', methods=['POST'])
@admin_required
def delete_branch(bid):
    conn = get_db()
    conn.execute('DELETE FROM branches WHERE id=?', (bid,))
    conn.commit()
    conn.close()
    flash('Branch deleted!', 'success')
    return redirect(url_for('branches'))


# ── Users (admin only) ─────────────────────────────────
@app.route('/users')
@admin_required
def users():
    conn = get_db()
    rows = conn.execute(f'''
        SELECT u.*, b.name AS branch_name,
          EXISTS(SELECT 1 FROM user_sessions s
                 WHERE s.user_id=u.id AND s.logout_at IS NULL
                   AND s.last_seen >= DATE_SUB(NOW(), INTERVAL {IDLE_SECONDS} SECOND)) AS is_online
        FROM users u LEFT JOIN branches b ON u.branch_id=b.id
        ORDER BY u.full_name
    ''').fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('users.html', users=rows, branches=branches)

@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO users (full_name, username, password, role, branch_id)
            VALUES (?,?,?,?,?)
        ''', (request.form['full_name'].strip(), request.form['username'].strip(),
              hash_password(request.form['password']),
              request.form['role'], request.form['branch_id']))
        conn.commit()
        flash(f'User "{request.form["username"]}" added. They can log in now.', 'success')
    except Exception:
        flash('Error: that username already exists.', 'error')
    conn.close()
    return redirect(url_for('users'))


@app.route('/users/edit/<int:uid>', methods=['POST'])
@admin_required
def edit_user(uid):
    if uid == session['user_id'] and request.form['role'] != 'admin':
        flash('You cannot remove your own admin role.', 'error')
        return redirect(url_for('users'))
    conn = get_db()
    try:
        conn.execute('''
            UPDATE users SET full_name=?, username=?, role=?, branch_id=?
            WHERE id=?
        ''', (request.form['full_name'].strip(), request.form['username'].strip(),
              request.form['role'], int(request.form['branch_id']) or None, uid))
        conn.commit()
        flash('User updated!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'error')
    conn.close()
    return redirect(url_for('users'))


@app.route('/users/change_password/<int:uid>', methods=['POST'])
@admin_required
def change_password(uid):
    new_pass = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if len(new_pass) < 4:
        flash('Password must be at least 4 characters!', 'error')
    elif new_pass != confirm:
        flash('Passwords do not match!', 'error')
    else:
        conn = get_db()
        conn.execute('UPDATE users SET password=? WHERE id=?',
                     (hash_password(new_pass), uid))
        conn.commit()
        user = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        conn.close()
        flash(f'Password changed for {user["username"]}!', 'success')
    return redirect(url_for('users'))


@app.route('/users/delete/<int:uid>', methods=['POST'])
@admin_required
def delete_user(uid):
    if uid == session['user_id']:
        flash('You cannot delete your own account!', 'error')
        return redirect(url_for('users'))
    conn = get_db()
    user = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    if user and user['username'] == 'admin':
        flash('Cannot delete the default admin account!', 'error')
    else:
        conn.execute('DELETE FROM users WHERE id=?', (uid,))
        conn.commit()
        flash('User deleted!', 'success')
    conn.close()
    return redirect(url_for('users'))


@app.route('/users/toggle/<int:uid>', methods=['POST'])
@admin_required
def toggle_user(uid):
    if uid == session['user_id']:
        flash('You cannot disable your own account!', 'error')
        return redirect(url_for('users'))
    conn = get_db()
    conn.execute('UPDATE users SET active=1-active WHERE id=?', (uid,))
    conn.commit()
    conn.close()
    flash('User status updated! A disabled user is signed out within 30 seconds.', 'success')
    return redirect(url_for('users'))


@app.route('/profile/change_password', methods=['POST'])
@login_required
def change_own_password():
    conn = get_db()
    user = conn.execute('SELECT password FROM users WHERE id=?',
                        (session['user_id'],)).fetchone()
    current = request.form.get('current_password', '')
    new_pass = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if user['password'] != hash_password(current):
        flash('Current password is incorrect!', 'error')
    elif new_pass != confirm:
        flash('New passwords do not match!', 'error')
    elif len(new_pass) < 4:
        flash('Password must be at least 4 characters!', 'error')
    else:
        conn.execute('UPDATE users SET password=? WHERE id=?',
                     (hash_password(new_pass), session['user_id']))
        conn.commit()
        flash('Your password has been changed!', 'success')
    conn.close()
    return redirect(url_for('users'))


# ── Departments ────────────────────────────────────────
@app.route('/departments')
@login_required
def departments():
    conn = get_db()
    bd = bf('d.branch_id')
    depts = conn.execute(f'''
        SELECT d.*, b.name AS branch_name, COUNT(c.id) AS cat_count
        FROM departments d
        LEFT JOIN branches b ON d.branch_id=b.id
        LEFT JOIN categories c ON c.department_id=d.id
        WHERE 1=1 {bd}
        GROUP BY d.id, b.name
        ORDER BY d.name
    ''').fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('departments.html', depts=depts, branches=branches)


@app.route('/departments/add', methods=['POST'])
@login_required
def add_department():
    conn = get_db()
    conn.execute('INSERT INTO departments (name, description, branch_id) VALUES (?,?,?)',
                 (request.form['name'], request.form.get('description', ''),
                  request.form['branch_id']))
    conn.commit()
    conn.close()
    flash('Department added!', 'success')
    return redirect(url_for('departments'))


@app.route('/departments/delete/<int:did>', methods=['POST'])
@login_required
def delete_department(did):
    conn = get_db()
    conn.execute('DELETE FROM departments WHERE id=?', (did,))
    conn.commit()
    conn.close()
    flash('Department deleted!', 'success')
    return redirect(url_for('departments'))


# ── Categories ─────────────────────────────────────────
@app.route('/categories')
@login_required
def categories():
    conn = get_db()
    bd = bf('d.branch_id')
    cats = conn.execute(f'''
        SELECT c.*, d.name AS dept_name, COUNT(p.id) AS product_count
        FROM categories c
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN products p ON p.category_id=c.id AND p.active=1
        WHERE 1=1 {bd}
        GROUP BY c.id, d.name
        ORDER BY c.name
    ''').fetchall()
    depts = conn.execute(
        f'SELECT * FROM departments d WHERE 1=1 {bd} ORDER BY d.name').fetchall()
    conn.close()
    return render_template('categories.html', cats=cats, depts=depts)


@app.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    conn = get_db()
    conn.execute('INSERT INTO categories (name, department_id, description) VALUES (?,?,?)',
                 (request.form['name'], request.form['department_id'],
                  request.form.get('description', '')))
    conn.commit()
    conn.close()
    flash('Category added!', 'success')
    return redirect(url_for('categories'))


@app.route('/categories/delete/<int:cid>', methods=['POST'])
@login_required
def delete_category(cid):
    conn = get_db()
    conn.execute('DELETE FROM categories WHERE id=?', (cid,))
    conn.commit()
    conn.close()
    flash('Category deleted!', 'success')
    return redirect(url_for('categories'))


# ── Products ───────────────────────────────────────────
@app.route('/products')
@login_required
def products():
    conn = get_db()
    search = request.args.get('q', '')
    cat_id = request.args.get('cat', '')
    dept_id = request.args.get('dept', '')
    bp = bf('p.branch_id')

    query = f'''
        SELECT p.*, c.name AS cat_name, d.name AS dept_name, b.name AS branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.active=1 {bp}'''
    params = []
    if search:
        query += ' AND (p.name LIKE ? OR p.barcode LIKE ?)'
        params += [f'%{search}%', f'%{search}%']
    if cat_id:
        query += ' AND p.category_id=?'
        params.append(cat_id)
    if dept_id:
        query += ' AND d.id=?'
        params.append(dept_id)
    query += ' ORDER BY p.name'

    prods = conn.execute(query, params).fetchall()
    cats = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    depts = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('products.html', prods=prods, cats=cats, depts=depts,
                           branches=branches, search=search,
                           cat_id=cat_id, dept_id=dept_id)


@app.route('/products/add', methods=['POST'])
@login_required
def add_product():
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO products
            (name, barcode, category_id, branch_id, buying_price,
             selling_price, stock_qty, min_stock, unit, description)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', (request.form['name'], request.form.get('barcode') or None,
              request.form['category_id'], request.form['branch_id'],
              float(request.form.get('buying_price', 0)),
              float(request.form.get('selling_price', 0)),
              int(request.form.get('stock_qty', 0)),
              int(request.form.get('min_stock', 5)),
              request.form.get('unit', 'pcs'),
              request.form.get('description', '')))
        conn.commit()
        flash('Product added!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'error')
    conn.close()
    return redirect(url_for('products'))


@app.route('/products/edit/<int:pid>', methods=['GET', 'POST'])
@login_required
def edit_product(pid):
    conn = get_db()
    if request.method == 'POST':
        try:
            conn.execute('''
                UPDATE products SET name=?, barcode=?, category_id=?, branch_id=?,
                    buying_price=?, selling_price=?, stock_qty=?, min_stock=?,
                    unit=?, description=?
                WHERE id=?
            ''', (request.form['name'], request.form.get('barcode') or None,
                  request.form['category_id'], request.form['branch_id'],
                  float(request.form.get('buying_price', 0)),
                  float(request.form.get('selling_price', 0)),
                  int(request.form.get('stock_qty', 0)),
                  int(request.form.get('min_stock', 5)),
                  request.form.get('unit', 'pcs'),
                  request.form.get('description', ''), pid))
            conn.commit()
            flash('Product updated!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'error')
        conn.close()
        return redirect(url_for('products'))
    prod = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    cats = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    depts = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('edit_product.html', prod=prod, cats=cats,
                           depts=depts, branches=branches)


@app.route('/products/delete/<int:pid>', methods=['POST'])
@login_required
def delete_product(pid):
    conn = get_db()
    conn.execute('UPDATE products SET active=0 WHERE id=?', (pid,))
    conn.commit()
    conn.close()
    flash('Product removed!', 'success')
    return redirect(url_for('products'))


# ── Stock ──────────────────────────────────────────────
@app.route('/stock')
@login_required
def stock():
    conn = get_db()
    bp, bs = bf('p.branch_id'), bf('s.branch_id')
    base = '''
        SELECT p.*, c.name AS cat_name, d.name AS dept_name, b.name AS branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.active=1 '''
    low_stock = conn.execute(
        f'{base} AND p.stock_qty<=p.min_stock {bp} ORDER BY p.stock_qty ASC').fetchall()
    all_stock = conn.execute(f'{base} {bp} ORDER BY p.name').fetchall()
    top_selling = conn.execute(f'''
        SELECT p.name, c.name AS cat_name,
               SUM(si.quantity) AS total_sold,
               SUM(si.total_price) AS total_revenue
        FROM sale_items si
        JOIN products p ON si.product_id=p.id
        JOIN sales s ON si.sale_id=s.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE 1=1 {bs}
        GROUP BY p.id, p.name, c.name
        ORDER BY total_sold DESC LIMIT 10
    ''').fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('stock.html', low_stock=low_stock, all_stock=all_stock,
                           top_selling=top_selling, branches=branches)


@app.route('/stock/adjust/<int:pid>', methods=['POST'])
@login_required
def adjust_stock(pid):
    conn = get_db()
    qty = int(request.form.get('qty', 0))
    if request.form.get('mode', 'add') == 'add':
        conn.execute('UPDATE products SET stock_qty=stock_qty+? WHERE id=?', (qty, pid))
    else:
        conn.execute('UPDATE products SET stock_qty=GREATEST(0, stock_qty-?) WHERE id=?',
                     (qty, pid))
    conn.commit()
    conn.close()
    flash('Stock updated!', 'success')
    return redirect(url_for('stock'))


@app.route('/stock/transfer', methods=['POST'])
@login_required
def transfer_stock():
    conn = get_db()
    pid = int(request.form['product_id'])
    from_b = int(request.form['from_branch'])
    to_b = int(request.form['to_branch'])
    qty = int(request.form['quantity'])
    src = conn.execute('SELECT * FROM products WHERE id=? AND branch_id=?',
                       (pid, from_b)).fetchone()
    if from_b == to_b or qty < 1 or not src or src['stock_qty'] < qty:
        conn.close()
        flash('Transfer failed: the product must belong to the "from" branch, '
              'the "to" branch must be different, and the quantity must be in stock.',
              'error')
        return redirect(url_for('stock'))

    conn.execute('UPDATE products SET stock_qty=stock_qty-? WHERE id=?', (qty, pid))
    dest = conn.execute(
        'SELECT id FROM products WHERE name=? AND branch_id=? AND active=1',
        (src['name'], to_b)).fetchone()
    if dest:
        conn.execute('UPDATE products SET stock_qty=stock_qty+? WHERE id=?',
                     (qty, dest['id']))
    else:
        conn.execute('''
            INSERT INTO products (name, category_id, branch_id, buying_price,
                selling_price, stock_qty, min_stock, unit, description)
            VALUES (?,?,?,?,?,?,?,?,?)
        ''', (src['name'], src['category_id'], to_b, src['buying_price'],
              src['selling_price'], qty, src['min_stock'], src['unit'],
              src['description']))
    conn.execute('''
        INSERT INTO stock_transfers
        (product_id, from_branch, to_branch, quantity, transferred_by)
        VALUES (?,?,?,?,?)
    ''', (pid, from_b, to_b, qty, session['user_id']))
    conn.commit()
    conn.close()
    flash(f'Transferred {qty} units!', 'success')
    return redirect(url_for('stock'))


# ── Customers ──────────────────────────────────────────
@app.route('/customers')
@login_required
def customers():
    conn = get_db()
    custs = conn.execute(f'''
        SELECT c.*, b.name AS branch_name
        FROM customers c LEFT JOIN branches b ON c.branch_id=b.id
        WHERE 1=1 {bf('c.branch_id')}
        ORDER BY c.full_name
    ''').fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('customers.html', custs=custs, branches=branches)


@app.route('/customers/add', methods=['POST'])
@login_required
def add_customer():
    conn = get_db()
    conn.execute('''
        INSERT INTO customers (full_name, phone, email, address, debit_limit, branch_id)
        VALUES (?,?,?,?,?,?)
    ''', (request.form['full_name'], request.form.get('phone', ''),
          request.form.get('email', ''), request.form.get('address', ''),
          float(request.form.get('debit_limit', 0)), request.form['branch_id']))
    conn.commit()
    conn.close()
    flash('Customer added!', 'success')
    return redirect(url_for('customers'))


@app.route('/customers/pay/<int:cid>', methods=['POST'])
@login_required
def customer_pay(cid):
    conn = get_db()
    amount = float(request.form.get('amount', 0))
    conn.execute(
        'UPDATE customers SET debit_balance=GREATEST(0, debit_balance-?) WHERE id=?',
        (amount, cid))
    conn.commit()
    conn.close()
    flash(f'Payment of TZS {amount:,.0f} recorded!', 'success')
    return redirect(url_for('customers'))


# ── POS ────────────────────────────────────────────────
@app.route('/pos')
@login_required
def pos():
    conn = get_db()
    products = conn.execute(f'''
        SELECT p.*, c.name AS cat_name, d.name AS dept_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        WHERE p.active=1 AND p.stock_qty>0 {bf('p.branch_id')}
        ORDER BY p.name
    ''').fetchall()
    customers = conn.execute(f'''
        SELECT * FROM customers c WHERE 1=1 {bf('c.branch_id')} ORDER BY c.full_name
    ''').fetchall()
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    conn.close()
    return render_template('pos.html', products=products,
                           customers=customers, categories=categories)


@app.route('/pos/checkout', methods=['POST'])
@login_required
def checkout():
    data = request.get_json()
    cart = data.get('cart', [])
    payment = data.get('payment_type', 'cash')
    paid_amount = float(data.get('paid_amount', 0))
    customer_id = data.get('customer_id') or None
    notes = data.get('notes', '')
    if not cart:
        return jsonify({'success': False, 'message': 'Cart is empty!'})

    conn = get_db()
    branch_id = session.get('branch_id') or 1
    total = sum(float(i['price']) * int(i['qty']) for i in cart)
    change = max(0, paid_amount - total)

    for item in cart:
        prod = conn.execute('SELECT stock_qty, name FROM products WHERE id=?',
                            (item['id'],)).fetchone()
        if not prod or prod['stock_qty'] < int(item['qty']):
            conn.close()
            return jsonify({'success': False,
                            'message': f'Insufficient stock for {prod["name"] if prod else "product"}!'})

    if payment == 'credit':
        if not customer_id:
            conn.close()
            return jsonify({'success': False,
                            'message': 'Select a customer for credit sale!'})
        cust = conn.execute('SELECT * FROM customers WHERE id=?',
                            (customer_id,)).fetchone()
        if cust['debit_limit'] > 0 and cust['debit_balance'] + total > cust['debit_limit']:
            conn.close()
            return jsonify({'success': False,
                            'message': f'Debit limit exceeded! Limit: TZS {cust["debit_limit"]:,.0f}'})
        paid_amount = 0

    receipt_no = generate_receipt_no()
    cur = conn.execute('''
        INSERT INTO sales
        (receipt_no, branch_id, user_id, customer_id, total_amount,
         paid_amount, change_amount, payment_type, notes)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', (receipt_no, branch_id, session['user_id'], customer_id,
          total, paid_amount, change, payment, notes))
    sale_id = cur.lastrowid

    items_data = []
    for item in cart:
        qty, price = int(item['qty']), float(item['price'])
        conn.execute('''
            INSERT INTO sale_items (sale_id, product_id, quantity, unit_price, total_price)
            VALUES (?,?,?,?,?)
        ''', (sale_id, item['id'], qty, price, price * qty))
        conn.execute('UPDATE products SET stock_qty=stock_qty-? WHERE id=?',
                     (qty, item['id']))
        name = conn.execute('SELECT name FROM products WHERE id=?',
                            (item['id'],)).fetchone()['name']
        items_data.append({'name': name, 'qty': qty, 'price': price,
                           'total': price * qty})

    if payment == 'credit' and customer_id:
        conn.execute(
            'UPDATE customers SET debit_balance=debit_balance+? WHERE id=?',
            (total, customer_id))

    conn.commit()
    conn.close()
    return jsonify({
        'success': True, 'receipt_no': receipt_no, 'total': total,
        'paid': paid_amount, 'change': change, 'payment': payment,
        'items': items_data, 'cashier': session['full_name'],
        'branch': session['branch_name'],
        'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


# ── Sales ──────────────────────────────────────────────
@app.route('/sales')
@login_required
def sales():
    conn = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    date_from = request.args.get('from', today)
    date_to = request.args.get('to', today)
    bs = bf('s.branch_id')
    sale_list = conn.execute(f'''
        SELECT s.*, u.full_name AS cashier, b.name AS branch_name,
               c.full_name AS customer_name
        FROM sales s
        LEFT JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON s.branch_id=b.id
        LEFT JOIN customers c ON s.customer_id=c.id
        WHERE {dr('s.created_at')} {bs}
        ORDER BY s.created_at DESC
    ''', (date_from, date_to)).fetchall()
    totals = conn.execute(f'''
        SELECT COALESCE(SUM(total_amount),0) AS total, COUNT(*) AS `count`
        FROM sales s
        WHERE {dr('s.created_at')} {bs}
    ''', (date_from, date_to)).fetchone()
    conn.close()
    return render_template('sales.html', sale_list=sale_list, totals=totals,
                           date_from=date_from, date_to=date_to)


@app.route('/sales/<int:sid>')
@login_required
def sale_detail(sid):
    conn = get_db()
    sale = conn.execute('''
        SELECT s.*, u.full_name AS cashier, b.name AS branch_name,
               c.full_name AS customer_name
        FROM sales s
        LEFT JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON s.branch_id=b.id
        LEFT JOIN customers c ON s.customer_id=c.id
        WHERE s.id=?
    ''', (sid,)).fetchone()
    items = conn.execute('''
        SELECT si.*, p.name AS product_name, p.unit
        FROM sale_items si JOIN products p ON si.product_id=p.id
        WHERE si.sale_id=?
    ''', (sid,)).fetchall()
    conn.close()
    return render_template('sale_detail.html', sale=sale, items=items)


# ── Expenses ───────────────────────────────────────────
@app.route('/expenses')
@login_required
def expenses():
    conn = get_db()
    month = request.args.get('from', datetime.now().strftime('%Y-%m'))
    first = month + '-01'
    be_e, be = bf('e.branch_id'), bf('branch_id')
    exps = conn.execute(f'''
        SELECT e.*, b.name AS branch_name, u.full_name AS recorded_by_name
        FROM expenses e
        LEFT JOIN branches b ON e.branch_id=b.id
        LEFT JOIN users u ON e.recorded_by=u.id
        WHERE {mr('e.created_at')} {be_e}
        ORDER BY e.created_at DESC
    ''', (first, first)).fetchall()
    total_exp = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0) FROM expenses
        WHERE {mr('created_at')} {be}
    ''', (first, first)).fetchone()[0]
    by_cat = conn.execute(f'''
        SELECT category, COALESCE(SUM(amount),0) AS total
        FROM expenses
        WHERE {mr('created_at')} {be}
        GROUP BY category ORDER BY total DESC
    ''', (first, first)).fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('expenses.html', exps=exps, total_exp=total_exp,
                           by_cat=by_cat, month=month, branches=branches)


@app.route('/expenses/add', methods=['POST'])
@login_required
def add_expense():
    conn = get_db()
    conn.execute('''
        INSERT INTO expenses (branch_id, category, description, amount, recorded_by)
        VALUES (?,?,?,?,?)
    ''', (request.form.get('branch_id') or session.get('branch_id'),
          request.form['category'], request.form['description'],
          float(request.form['amount']), session['user_id']))
    conn.commit()
    conn.close()
    flash('Expense recorded!', 'success')
    return redirect(url_for('expenses'))


@app.route('/expenses/delete/<int:eid>', methods=['POST'])
@login_required
def delete_expense(eid):
    conn = get_db()
    conn.execute('DELETE FROM expenses WHERE id=?', (eid,))
    conn.commit()
    conn.close()
    flash('Expense deleted!', 'success')
    return redirect(url_for('expenses'))


# ── Reports ────────────────────────────────────────────
def generate_expense_advice(revenue, gross_profit, net_profit,
                            total_expenses, margin, exp_by_cat):
    if revenue == 0:
        return [{'type': 'info', 'title': 'No sales data yet',
                 'message': 'Record some sales to get financial advice.'}]
    advice = []
    exp_ratio = total_expenses / revenue * 100
    if net_profit < 0:
        advice.append({'type': 'danger', 'title': 'Business is running at a loss!',
            'message': f'Losing TZS {abs(net_profit):,.0f}. Reduce expenses or raise prices now.'})
    elif net_profit < gross_profit * 0.1:
        advice.append({'type': 'warning', 'title': 'Net profit is very low',
            'message': f'Net margin is only {net_profit / revenue * 100:.1f}%. Reduce operating costs.'})
    else:
        advice.append({'type': 'success', 'title': 'Business is profitable!',
            'message': f'Net profit TZS {net_profit:,.0f} ({net_profit / revenue * 100:.1f}% margin).'})
    if exp_ratio > 40:
        advice.append({'type': 'danger', 'title': 'Expenses are too high',
            'message': f'Expenses are {exp_ratio:.1f}% of revenue. Target below 30%. Minimize them.'})
    elif exp_ratio > 25:
        advice.append({'type': 'warning', 'title': 'Expenses are moderate',
            'message': f'Expenses are {exp_ratio:.1f}% of revenue. Look for areas to cut.'})
    else:
        advice.append({'type': 'success', 'title': 'Expenses well controlled',
            'message': f'Expenses are only {exp_ratio:.1f}% of revenue. Keep it the same.'})
    if margin < 10:
        advice.append({'type': 'danger', 'title': 'Gross margin critically low',
            'message': f'Only {margin:.1f}% gross margin. Review buying or selling prices.'})
    elif margin < 20:
        advice.append({'type': 'warning', 'title': 'Gross margin needs improvement',
            'message': f'{margin:.1f}% margin. Negotiate better supplier prices.'})
    else:
        advice.append({'type': 'success', 'title': 'Good gross margin',
            'message': f'{margin:.1f}% gross margin is healthy.'})
    if exp_by_cat and exp_by_cat[0]['total'] > revenue * 0.15:
        top = exp_by_cat[0]
        advice.append({'type': 'warning',
            'title': f'High spending on {top["category"]}',
            'message': f'TZS {top["total"]:,.0f} on {top["category"]} '
                       f'({top["total"] / revenue * 100:.1f}% of revenue). Consider reducing.'})
    return advice


@app.route('/reports')
@login_required
def reports():
    conn = get_db()
    period = request.args.get('period', 'daily')
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if period == 'daily':
        date_from = date_to = today
    elif period == 'weekly':
        date_from, date_to = (now - dt.timedelta(days=7)).strftime('%Y-%m-%d'), today
    elif period == 'monthly':
        date_from, date_to = now.strftime('%Y-%m-01'), today
    elif period == 'yearly':
        date_from, date_to = now.strftime('%Y-01-01'), today
    else:
        date_from = request.args.get('from', today)
        date_to = request.args.get('to', today)

    bs, be = bf('s.branch_id'), bf('branch_id')
    pl = pnl(conn, date_from, date_to)

    daily_sales = conn.execute(f'''
        SELECT DATE(s.created_at) AS `day`,
               COALESCE(SUM(s.total_amount),0) AS total, COUNT(*) AS txns
        FROM sales s
        WHERE {dr('s.created_at')} {bs}
        GROUP BY DATE(s.created_at) ORDER BY `day`
    ''', (date_from, date_to)).fetchall()

    top_products = conn.execute(f'''
        SELECT p.name, c.name AS cat_name,
               SUM(si.quantity) AS qty_sold,
               SUM(si.total_price) AS revenue,
               SUM(si.quantity*p.buying_price) AS cost,
               SUM(si.total_price)-SUM(si.quantity*p.buying_price) AS profit
        FROM sale_items si
        JOIN sales s ON si.sale_id=s.id
        JOIN products p ON si.product_id=p.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE {dr('s.created_at')} {bs}
        GROUP BY p.id, p.name, c.name
        ORDER BY qty_sold DESC LIMIT 10
    ''', (date_from, date_to)).fetchall()

    top_categories = conn.execute(f'''
        SELECT c.name AS cat_name, d.name AS dept_name,
               SUM(si.quantity) AS qty_sold, SUM(si.total_price) AS revenue
        FROM sale_items si
        JOIN sales s ON si.sale_id=s.id
        JOIN products p ON si.product_id=p.id
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        WHERE {dr('s.created_at')} {bs}
        GROUP BY c.id, c.name, d.name
        ORDER BY revenue DESC LIMIT 8
    ''', (date_from, date_to)).fetchall()

    exp_by_cat = conn.execute(f'''
        SELECT category, COALESCE(SUM(amount),0) AS total
        FROM expenses
        WHERE {dr('created_at')} {be}
        GROUP BY category ORDER BY total DESC
    ''', (date_from, date_to)).fetchall()
    conn.close()

    advice = generate_expense_advice(pl['revenue'], pl['gross_profit'],
                                     pl['net_profit'], pl['expenses'],
                                     pl['margin'], exp_by_cat)
    return render_template('reports.html',
        period=period, date_from=date_from, date_to=date_to,
        revenue=pl['revenue'], cost=pl['cost'],
        gross_profit=pl['gross_profit'], net_profit=pl['net_profit'],
        total_expenses=pl['expenses'], margin=pl['margin'],
        daily_sales=daily_sales, top_products=top_products,
        top_categories=top_categories, exp_by_cat=exp_by_cat,
        advice=advice, transactions=pl['transactions'])


@app.route('/reports/pdf')
@login_required
def report_pdf():
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer)
        from reportlab.lib.styles import getSampleStyleSheet

        today = datetime.now().strftime('%Y-%m-%d')
        date_from = request.args.get('from', today)
        date_to = request.args.get('to', today)
        conn = get_db()
        bs, be = bf('s.branch_id'), bf('branch_id')
        pl = pnl(conn, date_from, date_to)
        tp = conn.execute(f'''
            SELECT p.name, SUM(si.quantity) AS qty, SUM(si.total_price) AS rev,
                   SUM(si.total_price)-SUM(si.quantity*p.buying_price) AS profit
            FROM sale_items si
            JOIN sales s ON si.sale_id=s.id
            JOIN products p ON si.product_id=p.id
            WHERE {dr('s.created_at')} {bs}
            GROUP BY p.id, p.name ORDER BY qty DESC LIMIT 15
        ''', (date_from, date_to)).fetchall()
        exp_cats = conn.execute(f'''
            SELECT category, COALESCE(SUM(amount),0) AS total
            FROM expenses WHERE {dr('created_at')} {be}
            GROUP BY category ORDER BY total DESC
        ''', (date_from, date_to)).fetchall()
        conn.close()

        revenue, net = pl['revenue'], pl['net_profit']
        pct = lambda v: f'{(v / revenue * 100) if revenue else 0:.1f}%'

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=40, bottomMargin=40)
        styl = getSampleStyleSheet()
        els = [
            Paragraph('NODDY STORE - FINANCIAL REPORT', styl['Title']),
            Paragraph(f'Period: {date_from} to {date_to} | Branch: {session["branch_name"]} | '
                      f'Generated by: {session["full_name"]} | '
                      f'{datetime.now().strftime("%Y-%m-%d %H:%M")}', styl['Normal']),
            Spacer(1, 16), Paragraph('Profit & Loss Statement', styl['Heading2'])]

        rows = [['Item', 'Amount (TZS)', 'Notes'],
                ['Total Revenue', f'{revenue:,.0f}', f'{pl["transactions"]} transactions'],
                ['Cost of Goods Sold', f'{pl["cost"]:,.0f}', f'{pct(pl["cost"])} of revenue'],
                ['Gross Profit', f'{pl["gross_profit"]:,.0f}', f'{pl["margin"]:.1f}% margin'],
                ['Total Expenses', f'{pl["expenses"]:,.0f}', f'{pct(pl["expenses"])} of revenue'],
                ['NET PROFIT / LOSS', f'{net:,.0f}', 'PROFIT' if net >= 0 else 'LOSS']]
        t = Table(rows, colWidths=[200, 150, 130])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BACKGROUND', (0, 5), (-1, 5),
             colors.HexColor('#1a4a1a') if net >= 0 else colors.HexColor('#4a1a1a')),
            ('TEXTCOLOR', (0, 5), (-1, 5), colors.white),
            ('FONTNAME', (0, 5), (-1, 5), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT')]))
        els += [t, Spacer(1, 20)]

        if exp_cats:
            els.append(Paragraph('Expenses by Category', styl['Heading2']))
            data = [['Category', 'Amount (TZS)', '% of Revenue']]
            data += [[e['category'], f'{e["total"]:,.0f}', pct(e['total'])] for e in exp_cats]
            t2 = Table(data, colWidths=[200, 150, 130])
            t2.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#c0392b')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ALIGN', (1, 0), (2, -1), 'RIGHT')]))
            els += [t2, Spacer(1, 20)]

        if tp:
            els.append(Paragraph('Top Products', styl['Heading2']))
            data = [['#', 'Product', 'Qty Sold', 'Revenue (TZS)', 'Profit (TZS)']]
            for i, p in enumerate(tp, 1):
                data.append([str(i), p['name'], str(p['qty']),
                             f'{p["rev"]:,.0f}', f'{p["profit"]:,.0f}'])
            t3 = Table(data, colWidths=[30, 180, 70, 110, 90])
            t3.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f39c12')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ALIGN', (2, 0), (4, -1), 'RIGHT')]))
            els.append(t3)

        doc.build(els)
        buf.seek(0)
        return send_file(buf, mimetype='application/pdf', as_attachment=True,
                         download_name=f'noddy_report_{date_from}_{date_to}.pdf')
    except ImportError:
        flash('Run: pip install reportlab', 'error')
    except Exception as e:
        flash(f'PDF error: {e}', 'error')
    return redirect(url_for('reports'))


# ── Staff ──────────────────────────────────────────────
def get_staff_recommendation(conn):
    be = bf('branch_id')
    avg = conn.execute(f'''
        SELECT AVG(t.c) FROM (
            SELECT COUNT(*) AS c FROM sales
            WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) {be}
            GROUP BY DATE(created_at)
        ) t
    ''').fetchone()[0] or 0
    cnt = conn.execute(
        f"SELECT COUNT(*) FROM users WHERE active=1 AND role<>'admin' {be}").fetchone()[0]
    rec = max(1, round(avg / 20))
    if cnt < rec:
        status = 'increase'
        msg = f'Based on {avg:.0f} average daily transactions you need {rec} staff. Hire {rec - cnt} more.'
    elif cnt > rec + 2:
        status = 'reduce'
        msg = f'You have {cnt} staff for {avg:.0f} daily transactions. You can reduce by {cnt - rec} to save costs.'
    else:
        status = 'optimal'
        msg = f'Your {cnt} staff is right for {avg:.0f} daily transactions. No change needed.'
    return {'status': status, 'message': msg, 'current': cnt,
            'recommended': rec, 'avg_txns': round(avg, 1)}


@app.route('/staff')
@login_required
def staff():
    conn = get_db()
    bu, be = bf('u.branch_id'), bf('branch_id')
    staff_list = conn.execute(f'''
        SELECT u.*, b.name AS branch_name,
          COALESCE((SELECT sal.amount FROM salaries sal
                    WHERE sal.user_id=u.id
                      AND sal.`month`=DATE_FORMAT(NOW(),'%Y-%m')),0) AS salary,
          COALESCE((SELECT sal.paid FROM salaries sal
                    WHERE sal.user_id=u.id
                      AND sal.`month`=DATE_FORMAT(NOW(),'%Y-%m')),0) AS salary_paid,
          (SELECT COUNT(DISTINCT DATE(ss.login_at)) FROM user_sessions ss
           WHERE ss.user_id=u.id
             AND ss.login_at >= DATE_FORMAT(CURDATE(),'%Y-%m-01')) AS attendance_days,
          EXISTS(SELECT 1 FROM user_sessions ss
                 WHERE ss.user_id=u.id AND ss.logout_at IS NULL
                   AND ss.last_seen >= DATE_SUB(NOW(), INTERVAL {IDLE_SECONDS} SECOND)) AS is_online
        FROM users u
        LEFT JOIN branches b ON u.branch_id=b.id
        WHERE u.role<>'admin' {bu}
        ORDER BY u.full_name
    ''').fetchall()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    salary_total = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0) FROM salaries
        WHERE `month`=DATE_FORMAT(NOW(),'%Y-%m') {be}
    ''').fetchone()[0]
    attendance_today = conn.execute(f'''
        SELECT COUNT(DISTINCT ss.user_id)
        FROM user_sessions ss JOIN users u ON ss.user_id=u.id
        WHERE ss.login_at >= CURDATE() AND u.role<>'admin' {bu}
    ''').fetchone()[0]
    recommendation = get_staff_recommendation(conn)
    conn.close()
    return render_template('staff.html', staff_list=staff_list, branches=branches,
                           recommendation=recommendation,
                           salary_total=salary_total,
                           attendance_today=attendance_today)


@app.route('/staff/attendance', methods=['POST'])
@app.route('/staff/checkout/<int:uid>', methods=['POST'])
@login_required
def manual_attendance_disabled(uid=None):
    flash('Attendance is now automatic: it is recorded when staff sign in and out.', 'success')
    return redirect(url_for('attendance_report'))


@app.route('/staff/salary/set', methods=['POST'])
@login_required
def set_salary():
    conn = get_db()
    uid = request.form['user_id']
    u = conn.execute('SELECT branch_id FROM users WHERE id=?', (uid,)).fetchone()
    try:
        conn.execute('''
            INSERT INTO salaries (user_id, branch_id, amount, `month`)
            VALUES (?,?,?,DATE_FORMAT(NOW(),'%Y-%m'))
            ON DUPLICATE KEY UPDATE amount=VALUES(amount), branch_id=VALUES(branch_id)
        ''', (uid, (u['branch_id'] if u else None) or session.get('branch_id'),
              float(request.form['amount'])))
        conn.commit()
        flash('Salary set!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'error')
    conn.close()
    return redirect(url_for('staff'))


@app.route('/staff/salary/pay/<int:uid>', methods=['POST'])
@login_required
def pay_salary(uid):
    conn = get_db()
    person = conn.execute('SELECT full_name, branch_id FROM users WHERE id=?',
                          (uid,)).fetchone()
    sal = conn.execute('''
        SELECT amount, paid FROM salaries
        WHERE user_id=? AND `month`=DATE_FORMAT(NOW(),'%Y-%m')
    ''', (uid,)).fetchone()
    if not sal:
        flash('Set a salary for this staff member first.', 'error')
    elif sal['paid']:
        flash("This month's salary was already paid.", 'error')
    else:
        conn.execute('''
            UPDATE salaries SET paid=1, paid_at=NOW()
            WHERE user_id=? AND `month`=DATE_FORMAT(NOW(),'%Y-%m')
        ''', (uid,))
        conn.execute('''
            INSERT INTO expenses (branch_id, category, description, amount, recorded_by)
            VALUES (?,?,?,?,?)
        ''', (person['branch_id'] or session.get('branch_id'), 'Salaries',
              f'Salary - {person["full_name"]}', sal['amount'], session['user_id']))
        conn.commit()
        flash('Salary paid and recorded as an expense!', 'success')
    conn.close()
    return redirect(url_for('staff'))


def worked_seconds(spans):
    """Total time worked. Overlapping sessions (e.g. PC + phone) are merged."""
    total, cur_start, cur_end = 0, None, None
    for start, end in sorted(spans):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    if cur_end is not None:
        total += (cur_end - cur_start).total_seconds()
    return int(max(0, total))


@app.route('/staff/attendance/report')
@login_required
def attendance_report():
    conn = get_db()
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    month = request.args.get('month', now.strftime('%Y-%m'))
    first = month + '-01'
    if session.get('role') == 'cashier':          # cashiers only see themselves
        scope = f"AND u.id={int(session['user_id'])}"
    else:
        scope = bf('u.branch_id')

    rows = conn.execute(f'''
        SELECT s.user_id, u.full_name, b.name AS branch_name,
               DATE(s.login_at) AS `day`, s.login_at, s.end_reason,
               COALESCE(s.logout_at, s.last_seen) AS ended_at,
               {ACTIVE_SQL} AS is_active
        FROM user_sessions s
        JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON u.branch_id=b.id
        WHERE {mr('s.login_at')} {scope}
        ORDER BY s.login_at
    ''', (first, first)).fetchall()

    online = online_users(conn) if session.get('role') == 'admin' else []
    conn.close()

    fmt = '%Y-%m-%d %H:%M:%S'
    days = {}
    for r in rows:
        key = (r['user_id'], r['day'])
        d = days.get(key)
        if d is None:
            d = days[key] = {'date': r['day'], 'full_name': r['full_name'],
                             'branch_name': r['branch_name'] or '—',
                             'check_in': r['login_at'], 'last_end': '',
                             'reason': None, 'active': False, 'spans': []}
        d['spans'].append((datetime.strptime(r['login_at'], fmt),
                           datetime.strptime(r['ended_at'], fmt)))
        if r['is_active']:
            d['active'] = True
        if r['ended_at'] >= d['last_end']:
            d['last_end'], d['reason'] = r['ended_at'], r['end_reason']

    records = []
    for d in days.values():
        secs = worked_seconds(d['spans'])
        h, m = divmod(secs // 60, 60)
        if d['active']:
            status, check_out = 'Active', None
        else:
            status = 'Logged out' if d['reason'] == 'logout' else 'Timed out'
            check_out = d['last_end']
        records.append({'date': d['date'], 'full_name': d['full_name'],
                        'branch_name': d['branch_name'],
                        'check_in': d['check_in'], 'check_out': check_out,
                        'hours': f'{h}h {m:02d}m', 'status': status, 'secs': secs})
    records.sort(key=lambda x: x['full_name'])
    records.sort(key=lambda x: x['date'], reverse=True)

    today_rows = [x for x in records if x['date'] == today]
    th, tm = divmod(sum(x['secs'] for x in today_rows) // 60, 60)
    return render_template('attendance.html', records=records, month=month,
                           online=online, today_present=len(today_rows),
                           today_hours=f'{th}h {tm:02d}m')
@app.route('/staff/performance')
@login_required
def staff_performance():
    conn = get_db()
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    first = month + '-01'
    bs, bu = bf('s.branch_id'), bf('u.branch_id')
    perf = conn.execute(f'''
        SELECT u.id, u.full_name, u.role, b.name AS branch_name,
          (SELECT COUNT(*) FROM sales s
           WHERE s.user_id=u.id AND {mr('s.created_at')} {bs}) AS total_sales,
          (SELECT COALESCE(SUM(s.total_amount),0) FROM sales s
           WHERE s.user_id=u.id AND {mr('s.created_at')} {bs}) AS total_revenue,
          (SELECT COUNT(DISTINCT DATE(ss.login_at)) FROM user_sessions ss
           WHERE ss.user_id=u.id AND {mr('ss.login_at')}) AS days_present,
          COALESCE((SELECT sal.amount FROM salaries sal
                    WHERE sal.user_id=u.id AND sal.`month`=?),0) AS salary
        FROM users u
        LEFT JOIN branches b ON u.branch_id=b.id
        WHERE u.role<>'admin' AND u.active=1 {bu}
        ORDER BY total_revenue DESC
    ''', (first, first, first, first, first, first, month)).fetchall()
    conn.close()
    return render_template('staff_performance.html', performance=perf, month=month)


# ── Branch overview ────────────────────────────────────
@app.route('/branch/overview')
@login_required
def branch_overview():
    conn = get_db()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    branch_stats = []
    for b in branches:
        bid = b['id']
        ts = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) AS revenue, COUNT(*) AS transactions
            FROM sales WHERE branch_id=? AND created_at >= CURDATE()
        ''', (bid,)).fetchone()
        ms = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) AS revenue, COUNT(*) AS transactions
            FROM sales
            WHERE branch_id=? AND created_at >= DATE_FORMAT(CURDATE(),'%Y-%m-01')
        ''', (bid,)).fetchone()
        me = conn.execute('''
            SELECT COALESCE(SUM(amount),0) FROM expenses
            WHERE branch_id=? AND created_at >= DATE_FORMAT(CURDATE(),'%Y-%m-01')
        ''', (bid,)).fetchone()[0]
        mc = conn.execute('''
            SELECT COALESCE(SUM(si.quantity*p.buying_price),0)
            FROM sale_items si
            JOIN sales s ON si.sale_id=s.id
            JOIN products p ON si.product_id=p.id
            WHERE s.branch_id=? AND s.created_at >= DATE_FORMAT(CURDATE(),'%Y-%m-01')
        ''', (bid,)).fetchone()[0]
        sc = conn.execute(
            "SELECT COUNT(*) FROM users WHERE branch_id=? AND active=1 AND role<>'admin'",
            (bid,)).fetchone()[0]
        ls = conn.execute(
            'SELECT COUNT(*) FROM products '
            'WHERE branch_id=? AND stock_qty<=min_stock AND active=1', (bid,)).fetchone()[0]
        gross = ms['revenue'] - mc
        branch_stats.append({
            'branch': dict(b),
            'today_revenue': ts['revenue'], 'today_txns': ts['transactions'],
            'month_revenue': ms['revenue'], 'month_txns': ms['transactions'],
            'month_expenses': me, 'gross_profit': gross,
            'net_profit': gross - me, 'staff_count': sc, 'low_stock': ls})

    totals = {
        'today_revenue':  sum(x['today_revenue'] for x in branch_stats),
        'month_revenue':  sum(x['month_revenue'] for x in branch_stats),
        'month_expenses': sum(x['month_expenses'] for x in branch_stats),
        'net_profit':     sum(x['net_profit'] for x in branch_stats),
        'staff':          sum(x['staff_count'] for x in branch_stats),
        'low_stock':      sum(x['low_stock'] for x in branch_stats)}
    best = max(branch_stats, key=lambda x: x['month_revenue'], default=None)

    weekly = conn.execute('''
        SELECT DATE(s.created_at) AS `day`, b.name AS branch_name,
               COALESCE(SUM(s.total_amount),0) AS total
        FROM sales s JOIN branches b ON s.branch_id=b.id
        WHERE s.created_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
        GROUP BY DATE(s.created_at), b.name
        ORDER BY `day`
    ''').fetchall()
    debtors = conn.execute('''
        SELECT c.*, b.name AS branch_name
        FROM customers c LEFT JOIN branches b ON c.branch_id=b.id
        WHERE c.debit_balance>0
        ORDER BY c.debit_balance DESC LIMIT 10
    ''').fetchall()
    conn.close()
    return render_template('branch_overview.html', branch_stats=branch_stats,
                           totals=totals, best=best, weekly=weekly, debtors=debtors)


# ── Mobile money ───────────────────────────────────────
@app.route('/mobile_money')
@login_required
def mobile_money():
    conn = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    date_from = request.args.get('from', today)
    date_to = request.args.get('to', today)
    provider = request.args.get('provider', '')
    bm = bf('m.branch_id')

    pf, params = '', [date_from, date_to]
    if provider:
        pf = 'AND m.provider=?'
        params.append(provider)

    txns = conn.execute(f'''
        SELECT m.*, b.name AS branch_name, u.full_name AS recorded_by_name
        FROM mobile_money_transactions m
        LEFT JOIN branches b ON m.branch_id=b.id
        LEFT JOIN users u ON m.recorded_by=u.id
        WHERE {dr('m.created_at')} {bm} {pf}
        ORDER BY m.created_at DESC
    ''', params).fetchall()

    summary = conn.execute(f'''
        SELECT m.provider,
               COALESCE(SUM(CASE WHEN m.`type`='credit' THEN m.amount ELSE 0 END),0) AS total_credited,
               COALESCE(SUM(CASE WHEN m.`type`='debit'  THEN m.amount ELSE 0 END),0) AS total_debited,
               COUNT(*) AS `count`
        FROM mobile_money_transactions m
        WHERE {dr('m.created_at')} {bm}
        GROUP BY m.provider
    ''', (date_from, date_to)).fetchall()

    overall = conn.execute(f'''
        SELECT COALESCE(SUM(CASE WHEN m.`type`='credit' THEN m.amount ELSE 0 END),0) AS total_credited,
               COALESCE(SUM(CASE WHEN m.`type`='debit'  THEN m.amount ELSE 0 END),0) AS total_debited,
               COUNT(*) AS `count`
        FROM mobile_money_transactions m
        WHERE {dr('m.created_at')} {bm}
    ''', (date_from, date_to)).fetchone()

    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('mobile_money.html', txns=txns, summary=summary,
                           overall=overall, date_from=date_from, date_to=date_to,
                           provider=provider, branches=branches)


@app.route('/mobile_money/add', methods=['POST'])
@login_required
def add_mobile_money():
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO mobile_money_transactions
            (branch_id, provider, `type`, phone_number, amount,
             reference, description, recorded_by)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (request.form.get('branch_id') or session.get('branch_id'),
              request.form['provider'], request.form['type'],
              request.form.get('phone_number', ''),
              float(request.form['amount']),
              request.form.get('reference', ''),
              request.form.get('description', ''), session['user_id']))
        conn.commit()
        flash('Transaction recorded!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'error')
    conn.close()
    return redirect(url_for('mobile_money'))


@app.route('/mobile_money/delete/<int:mid>', methods=['POST'])
@login_required
def delete_mobile_money(mid):
    conn = get_db()
    conn.execute('DELETE FROM mobile_money_transactions WHERE id=?', (mid,))
    conn.commit()
    conn.close()
    flash('Transaction deleted!', 'success')
    return redirect(url_for('mobile_money'))


# ── API ────────────────────────────────────────────────
@app.route('/api/stats')
@login_required
def api_stats():
    conn = get_db()
    rows = conn.execute(f'''
        SELECT DATE(s.created_at) AS `day`, COALESCE(SUM(s.total_amount),0) AS total
        FROM sales s
        WHERE s.created_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY) {bf('s.branch_id')}
        GROUP BY DATE(s.created_at) ORDER BY `day`
    ''').fetchall()
    conn.close()
    return jsonify({'sales_week': [{'day': r['day'], 'total': r['total']} for r in rows]})


@app.route('/api/branch_chart')
@login_required
def api_branch_chart():
    conn = get_db()
    data = []
    for b in conn.execute('SELECT * FROM branches ORDER BY name').fetchall():
        rev = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) FROM sales
            WHERE branch_id=? AND created_at >= DATE_FORMAT(CURDATE(),'%Y-%m-01')
        ''', (b['id'],)).fetchone()[0]
        data.append({'name': b['name'], 'revenue': rev})
    conn.close()
    return jsonify(data)


@app.route('/api/product/<barcode>')
@login_required
def api_product(barcode):
    conn = get_db()
    p = conn.execute('SELECT * FROM products WHERE barcode=? AND active=1',
                     (barcode,)).fetchone()
    conn.close()
    if p:
        return jsonify({'found': True, 'id': p['id'], 'name': p['name'],
                        'price': p['selling_price'], 'stock': p['stock_qty'],
                        'unit': p['unit']})
    return jsonify({'found': False})


# ── Main ───────────────────────────────────────────────
if __name__ == '__main__':
    print('=' * 50)
    print('  NODDY STORE POS SYSTEM (MySQL)')
    print('=' * 50)
    init_db()
    print('\nOpen: http://localhost:5001\n')
    try:
        from waitress import serve          # fast production server
        serve(app, host='0.0.0.0', port=5001, threads=8)
    except ImportError:
        print('Tip: run  pip install waitress  for a faster server.\n')
        app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)