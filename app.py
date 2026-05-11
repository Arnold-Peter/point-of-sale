from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash)
from functools import wraps
from datetime import datetime
from database import init_db, get_db
from auth import verify_user, log_action, hash_password

app = Flask(__name__)
app.jinja_env.globals['enumerate'] = enumerate
app.secret_key = 'noddystore_secret_2024'

# ── Auth helpers ───────────────────────────────────────
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
        if session.get('role') not in ['admin', 'manager']:
            flash('Access denied!', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

# ── Auth routes ────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        user = verify_user(username, password)
        if user:
            session['user_id']     = user['id']
            session['username']    = user['username']
            session['full_name']   = user['full_name']
            session['role']        = user['role']
            session['branch_id']   = user['branch_id']
            session['branch_name'] = user['branch_name'] or 'All Branches'
            log_action(user['id'], user['username'], 'login')
            return redirect(url_for('dashboard'))
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    if 'user_id' in session:
        log_action(session['user_id'], session['username'], 'logout')
    session.clear()
    return redirect(url_for('login'))

# ── Dashboard ──────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    conn = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')

    # Stats — filter by branch unless admin
    if role == 'admin':
        total_products = conn.execute(
            'SELECT COUNT(*) FROM products WHERE active=1'
        ).fetchone()[0]
        total_sales_today = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) FROM sales
            WHERE DATE(created_at)=DATE('now')
        ''').fetchone()[0]
        total_customers = conn.execute(
            'SELECT COUNT(*) FROM customers'
        ).fetchone()[0]
        total_staff = conn.execute(
            'SELECT COUNT(*) FROM users WHERE active=1 AND role != "admin"'
        ).fetchone()[0]
        low_stock = conn.execute('''
            SELECT COUNT(*) FROM products
            WHERE stock_qty <= min_stock AND active=1
        ''').fetchone()[0]
        branches = conn.execute('SELECT * FROM branches').fetchall()
        recent_sales = conn.execute('''
            SELECT s.*, u.full_name as cashier,
                   b.name as branch_name
            FROM sales s
            LEFT JOIN users u ON s.user_id=u.id
            LEFT JOIN branches b ON s.branch_id=b.id
            ORDER BY s.created_at DESC LIMIT 8
        ''').fetchall()
    else:
        total_products = conn.execute(
            'SELECT COUNT(*) FROM products WHERE active=1 AND branch_id=?',
            (branch_id,)
        ).fetchone()[0]
        total_sales_today = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) FROM sales
            WHERE DATE(created_at)=DATE('now') AND branch_id=?
        ''', (branch_id,)).fetchone()[0]
        total_customers = conn.execute(
            'SELECT COUNT(*) FROM customers WHERE branch_id=?',
            (branch_id,)
        ).fetchone()[0]
        total_staff = conn.execute(
            'SELECT COUNT(*) FROM users WHERE active=1 AND branch_id=? AND role != "admin"',
            (branch_id,)
        ).fetchone()[0]
        low_stock = conn.execute('''
            SELECT COUNT(*) FROM products
            WHERE stock_qty <= min_stock AND active=1 AND branch_id=?
        ''', (branch_id,)).fetchone()[0]
        branches = conn.execute(
            'SELECT * FROM branches WHERE id=?', (branch_id,)
        ).fetchall()
        recent_sales = conn.execute('''
            SELECT s.*, u.full_name as cashier,
                   b.name as branch_name
            FROM sales s
            LEFT JOIN users u ON s.user_id=u.id
            LEFT JOIN branches b ON s.branch_id=b.id
            WHERE s.branch_id=?
            ORDER BY s.created_at DESC LIMIT 8
        ''', (branch_id,)).fetchall()

    conn.close()
    return render_template('dashboard.html',
        total_products=total_products,
        total_sales_today=total_sales_today,
        total_customers=total_customers,
        total_staff=total_staff,
        low_stock=low_stock,
        branches=branches,
        recent_sales=recent_sales
    )

# ── Branches ───────────────────────────────────────────
@app.route('/branches')
@login_required
def branches():
    conn = get_db()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('branches.html', branches=branches)

@app.route('/branches/add', methods=['POST'])
@login_required
def add_branch():
    conn = get_db()
    conn.execute('''INSERT INTO branches (name, location, phone)
                    VALUES (?,?,?)''',
                 (request.form['name'],
                  request.form.get('location',''),
                  request.form.get('phone','')))
    conn.commit()
    conn.close()
    flash('Branch added successfully!', 'success')
    return redirect(url_for('branches'))

@app.route('/branches/delete/<int:bid>', methods=['POST'])
@login_required
def delete_branch(bid):
    conn = get_db()
    conn.execute('DELETE FROM branches WHERE id=?', (bid,))
    conn.commit()
    conn.close()
    flash('Branch deleted!', 'success')
    return redirect(url_for('branches'))

# ── Users / Staff ──────────────────────────────────────
@app.route('/users')
@login_required
def users():
    conn = get_db()
    users = conn.execute('''
        SELECT u.*, b.name as branch_name
        FROM users u
        LEFT JOIN branches b ON u.branch_id=b.id
        ORDER BY u.full_name
    ''').fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('users.html',
                           users=users, branches=branches)

@app.route('/users/add', methods=['POST'])
@login_required
def add_user():
    conn = get_db()
    try:
        conn.execute('''INSERT INTO users
                        (full_name,username,password,role,branch_id)
                        VALUES (?,?,?,?,?)''',
                     (request.form['full_name'],
                      request.form['username'],
                      hash_password(request.form['password']),
                      request.form['role'],
                      request.form['branch_id']))
        conn.commit()
        flash('User added successfully!', 'success')
    except Exception as e:
        flash(f'Error: Username already exists!', 'error')
    conn.close()
    return redirect(url_for('users'))

@app.route('/users/toggle/<int:uid>', methods=['POST'])
@login_required
def toggle_user(uid):
    conn = get_db()
    conn.execute('''UPDATE users SET active = CASE
                    WHEN active=1 THEN 0 ELSE 1 END
                    WHERE id=?''', (uid,))
    conn.commit()
    conn.close()
    flash('User status updated!', 'success')
    return redirect(url_for('users'))

# ── API: dashboard stats ───────────────────────────────
@app.route('/api/stats')
@login_required
def api_stats():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')

    if role == 'admin':
        sales_week = conn.execute('''
            SELECT DATE(created_at) as day,
                   COALESCE(SUM(total_amount),0) as total
            FROM sales
            WHERE created_at >= DATE('now','-6 days')
            GROUP BY DATE(created_at)
            ORDER BY day
        ''').fetchall()
    else:
        sales_week = conn.execute('''
            SELECT DATE(created_at) as day,
                   COALESCE(SUM(total_amount),0) as total
            FROM sales
            WHERE created_at >= DATE('now','-6 days')
              AND branch_id=?
            GROUP BY DATE(created_at)
            ORDER BY day
        ''', (branch_id,)).fetchall()

    conn.close()
    return jsonify({
        'sales_week': [{'day': r['day'], 'total': r['total']}
                       for r in sales_week]
    })

# ── Departments ────────────────────────────────────────
@app.route('/departments')
@login_required
def departments():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    if role == 'admin':
        depts = conn.execute('''
            SELECT d.*, b.name as branch_name,
                   COUNT(c.id) as cat_count
            FROM departments d
            LEFT JOIN branches b ON d.branch_id=b.id
            LEFT JOIN categories c ON c.department_id=d.id
            GROUP BY d.id ORDER BY d.name
        ''').fetchall()
    else:
        depts = conn.execute('''
            SELECT d.*, b.name as branch_name,
                   COUNT(c.id) as cat_count
            FROM departments d
            LEFT JOIN branches b ON d.branch_id=b.id
            LEFT JOIN categories c ON c.department_id=d.id
            WHERE d.branch_id=?
            GROUP BY d.id ORDER BY d.name
        ''', (branch_id,)).fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('departments.html',
                           depts=depts, branches=branches)

@app.route('/departments/add', methods=['POST'])
@login_required
def add_department():
    conn = get_db()
    conn.execute('''INSERT INTO departments (name,description,branch_id)
                    VALUES (?,?,?)''',
                 (request.form['name'],
                  request.form.get('description',''),
                  request.form['branch_id']))
    conn.commit(); conn.close()
    flash('Department added!', 'success')
    return redirect(url_for('departments'))

@app.route('/departments/delete/<int:did>', methods=['POST'])
@login_required
def delete_department(did):
    conn = get_db()
    conn.execute('DELETE FROM departments WHERE id=?', (did,))
    conn.commit(); conn.close()
    flash('Department deleted!', 'success')
    return redirect(url_for('departments'))

# ── Categories ─────────────────────────────────────────
@app.route('/categories')
@login_required
def categories():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    if role == 'admin':
        cats = conn.execute('''
            SELECT c.*, d.name as dept_name,
                   COUNT(p.id) as product_count
            FROM categories c
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN products p ON p.category_id=c.id AND p.active=1
            GROUP BY c.id ORDER BY c.name
        ''').fetchall()
        depts = conn.execute('SELECT * FROM departments').fetchall()
    else:
        cats = conn.execute('''
            SELECT c.*, d.name as dept_name,
                   COUNT(p.id) as product_count
            FROM categories c
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN products p ON p.category_id=c.id AND p.active=1
            WHERE d.branch_id=?
            GROUP BY c.id ORDER BY c.name
        ''', (branch_id,)).fetchall()
        depts = conn.execute(
            'SELECT * FROM departments WHERE branch_id=?',
            (branch_id,)
        ).fetchall()
    conn.close()
    return render_template('categories.html',
                           cats=cats, depts=depts)

@app.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    conn = get_db()
    conn.execute('''INSERT INTO categories (name,department_id,description)
                    VALUES (?,?,?)''',
                 (request.form['name'],
                  request.form['department_id'],
                  request.form.get('description','')))
    conn.commit(); conn.close()
    flash('Category added!', 'success')
    return redirect(url_for('categories'))

@app.route('/categories/delete/<int:cid>', methods=['POST'])
@login_required
def delete_category(cid):
    conn = get_db()
    conn.execute('DELETE FROM categories WHERE id=?', (cid,))
    conn.commit(); conn.close()
    flash('Category deleted!', 'success')
    return redirect(url_for('categories'))

# ── Products ───────────────────────────────────────────
@app.route('/products')
@login_required
def products():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    search    = request.args.get('q','')
    cat_id    = request.args.get('cat','')
    dept_id   = request.args.get('dept','')

    query  = '''
        SELECT p.*, c.name as cat_name,
               d.name as dept_name, b.name as branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.active=1
    '''
    params = []

    if role != 'admin':
        query += ' AND p.branch_id=?'
        params.append(branch_id)
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

    if role == 'admin':
        cats  = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
        depts = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
        branches = conn.execute('SELECT * FROM branches').fetchall()
    else:
        depts = conn.execute(
            'SELECT * FROM departments WHERE branch_id=?',
            (branch_id,)
        ).fetchall()
        cats = conn.execute('''
            SELECT c.* FROM categories c
            JOIN departments d ON c.department_id=d.id
            WHERE d.branch_id=?
        ''', (branch_id,)).fetchall()
        branches = conn.execute(
            'SELECT * FROM branches WHERE id=?', (branch_id,)
        ).fetchall()

    conn.close()
    return render_template('products.html',
        prods=prods, cats=cats, depts=depts,
        branches=branches, search=search,
        cat_id=cat_id, dept_id=dept_id
    )

@app.route('/products/add', methods=['POST'])
@login_required
def add_product():
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO products
            (name,barcode,category_id,branch_id,buying_price,
             selling_price,stock_qty,min_stock,unit,description)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', (
            request.form['name'],
            request.form.get('barcode') or None,
            request.form['category_id'],
            request.form['branch_id'],
            float(request.form.get('buying_price', 0)),
            float(request.form.get('selling_price', 0)),
            int(request.form.get('stock_qty', 0)),
            int(request.form.get('min_stock', 5)),
            request.form.get('unit', 'pcs'),
            request.form.get('description', '')
        ))
        conn.commit()
        flash('Product added successfully!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    conn.close()
    return redirect(url_for('products'))

@app.route('/products/edit/<int:pid>', methods=['GET', 'POST'])
@login_required
def edit_product(pid):
    conn = get_db()
    if request.method == 'POST':
        conn.execute('''
            UPDATE products SET
            name=?, barcode=?, category_id=?, branch_id=?,
            buying_price=?, selling_price=?,
            stock_qty=?, min_stock=?, unit=?, description=?
            WHERE id=?
        ''', (
            request.form['name'],
            request.form.get('barcode') or None,
            request.form['category_id'],
            request.form['branch_id'],
            float(request.form.get('buying_price', 0)),
            float(request.form.get('selling_price', 0)),
            int(request.form.get('stock_qty', 0)),
            int(request.form.get('min_stock', 5)),
            request.form.get('unit', 'pcs'),
            request.form.get('description', ''),
            pid
        ))
        conn.commit()
        conn.close()
        flash('Product updated!', 'success')
        return redirect(url_for('products'))

    prod  = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    cats  = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    depts = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('edit_product.html',
                           prod=prod, cats=cats,
                           depts=depts, branches=branches)

@app.route('/products/delete/<int:pid>', methods=['POST'])
@login_required
def delete_product(pid):
    conn = get_db()
    conn.execute('UPDATE products SET active=0 WHERE id=?', (pid,))
    conn.commit(); conn.close()
    flash('Product removed!', 'success')
    return redirect(url_for('products'))

# ── Stock ──────────────────────────────────────────────
@app.route('/stock')
@login_required
def stock():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')

    if role == 'admin':
        low_stock = conn.execute('''
            SELECT p.*, c.name as cat_name,
                   d.name as dept_name, b.name as branch_name
            FROM products p
            LEFT JOIN categories c ON p.category_id=c.id
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN branches b ON p.branch_id=b.id
            WHERE p.stock_qty <= p.min_stock AND p.active=1
            ORDER BY p.stock_qty ASC
        ''').fetchall()
        all_stock = conn.execute('''
            SELECT p.*, c.name as cat_name,
                   d.name as dept_name, b.name as branch_name
            FROM products p
            LEFT JOIN categories c ON p.category_id=c.id
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN branches b ON p.branch_id=b.id
            WHERE p.active=1
            ORDER BY p.name
        ''').fetchall()
        top_selling = conn.execute('''
            SELECT p.name, c.name as cat_name,
                   SUM(si.quantity) as total_sold
            FROM sale_items si
            JOIN products p ON si.product_id=p.id
            LEFT JOIN categories c ON p.category_id=c.id
            GROUP BY p.id ORDER BY total_sold DESC LIMIT 10
        ''').fetchall()
    else:
        low_stock = conn.execute('''
            SELECT p.*, c.name as cat_name,
                   d.name as dept_name, b.name as branch_name
            FROM products p
            LEFT JOIN categories c ON p.category_id=c.id
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN branches b ON p.branch_id=b.id
            WHERE p.stock_qty <= p.min_stock
              AND p.active=1 AND p.branch_id=?
            ORDER BY p.stock_qty ASC
        ''', (branch_id,)).fetchall()
        all_stock = conn.execute('''
            SELECT p.*, c.name as cat_name,
                   d.name as dept_name, b.name as branch_name
            FROM products p
            LEFT JOIN categories c ON p.category_id=c.id
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN branches b ON p.branch_id=b.id
            WHERE p.active=1 AND p.branch_id=?
            ORDER BY p.name
        ''', (branch_id,)).fetchall()
        top_selling = conn.execute('''
            SELECT p.name, c.name as cat_name,
                   SUM(si.quantity) as total_sold
            FROM sale_items si
            JOIN products p ON si.product_id=p.id
            JOIN sales s ON si.sale_id=s.id
            LEFT JOIN categories c ON p.category_id=c.id
            WHERE s.branch_id=?
            GROUP BY p.id ORDER BY total_sold DESC LIMIT 10
        ''', (branch_id,)).fetchall()

    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('stock.html',
        low_stock=low_stock,
        all_stock=all_stock,
        top_selling=top_selling,
        branches=branches
    )

@app.route('/stock/adjust/<int:pid>', methods=['POST'])
@login_required
def adjust_stock(pid):
    conn = get_db()
    qty  = int(request.form.get('qty', 0))
    mode = request.form.get('mode', 'add')
    if mode == 'add':
        conn.execute(
            'UPDATE products SET stock_qty=stock_qty+? WHERE id=?',
            (qty, pid)
        )
    else:
        conn.execute('''
            UPDATE products
            SET stock_qty=MAX(0, stock_qty-?)
            WHERE id=?
        ''', (qty, pid))
    conn.commit(); conn.close()
    flash('Stock updated!', 'success')
    return redirect(url_for('stock'))

@app.route('/stock/transfer', methods=['POST'])
@login_required
def transfer_stock():
    conn = get_db()
    pid        = int(request.form['product_id'])
    from_b     = int(request.form['from_branch'])
    to_b       = int(request.form['to_branch'])
    qty        = int(request.form['quantity'])
    # Deduct from source
    conn.execute('''
        UPDATE products
        SET stock_qty=MAX(0,stock_qty-?)
        WHERE id=? AND branch_id=?
    ''', (qty, pid, from_b))
    # Add to destination (find same product at destination)
    dest = conn.execute('''
        SELECT id FROM products
        WHERE name=(SELECT name FROM products WHERE id=?)
          AND branch_id=?
    ''', (pid, to_b)).fetchone()
    if dest:
        conn.execute(
            'UPDATE products SET stock_qty=stock_qty+? WHERE id=?',
            (qty, dest['id'])
        )
    # Log transfer
    conn.execute('''
        INSERT INTO stock_transfers
        (product_id,from_branch,to_branch,quantity,transferred_by)
        VALUES (?,?,?,?,?)
    ''', (pid, from_b, to_b, qty, session['user_id']))
    conn.commit(); conn.close()
    flash(f'Transferred {qty} units successfully!', 'success')
    return redirect(url_for('stock'))

import random
import string

def generate_receipt_no():
    """Generate unique receipt number"""
    chars = string.ascii_uppercase + string.digits
    rand  = ''.join(random.choices(chars, k=6))
    return f'RCP-{datetime.now().strftime("%Y%m%d")}-{rand}'

# ── Customers ──────────────────────────────────────────
@app.route('/customers')
@login_required
def customers():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    if role == 'admin':
        custs = conn.execute('''
            SELECT c.*, b.name as branch_name
            FROM customers c
            LEFT JOIN branches b ON c.branch_id=b.id
            ORDER BY c.full_name
        ''').fetchall()
    else:
        custs = conn.execute('''
            SELECT c.*, b.name as branch_name
            FROM customers c
            LEFT JOIN branches b ON c.branch_id=b.id
            WHERE c.branch_id=?
            ORDER BY c.full_name
        ''', (branch_id,)).fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('customers.html',
                           custs=custs, branches=branches)

@app.route('/customers/add', methods=['POST'])
@login_required
def add_customer():
    conn = get_db()
    conn.execute('''
        INSERT INTO customers
        (full_name,phone,email,address,debit_limit,branch_id)
        VALUES (?,?,?,?,?,?)
    ''', (
        request.form['full_name'],
        request.form.get('phone',''),
        request.form.get('email',''),
        request.form.get('address',''),
        float(request.form.get('debit_limit',0)),
        request.form['branch_id']
    ))
    conn.commit(); conn.close()
    flash('Customer added!', 'success')
    return redirect(url_for('customers'))

@app.route('/customers/pay/<int:cid>', methods=['POST'])
@login_required
def customer_pay(cid):
    """Record a debit payment from customer"""
    conn   = get_db()
    amount = float(request.form.get('amount', 0))
    conn.execute('''
        UPDATE customers
        SET debit_balance = MAX(0, debit_balance - ?)
        WHERE id=?
    ''', (amount, cid))
    conn.commit(); conn.close()
    flash(f'Payment of TZS {amount:,.0f} recorded!', 'success')
    return redirect(url_for('customers'))

# ── POS Terminal ───────────────────────────────────────
@app.route('/pos')
@login_required
def pos():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')

    if role == 'admin':
        products = conn.execute('''
            SELECT p.*, c.name as cat_name,
                   d.name as dept_name
            FROM products p
            LEFT JOIN categories c ON p.category_id=c.id
            LEFT JOIN departments d ON c.department_id=d.id
            WHERE p.active=1 AND p.stock_qty > 0
            ORDER BY p.name
        ''').fetchall()
        customers = conn.execute(
            'SELECT * FROM customers ORDER BY full_name'
        ).fetchall()
    else:
        products = conn.execute('''
            SELECT p.*, c.name as cat_name,
                   d.name as dept_name
            FROM products p
            LEFT JOIN categories c ON p.category_id=c.id
            LEFT JOIN departments d ON c.department_id=d.id
            WHERE p.active=1 AND p.stock_qty > 0
              AND p.branch_id=?
            ORDER BY p.name
        ''', (branch_id,)).fetchall()
        customers = conn.execute(
            'SELECT * FROM customers WHERE branch_id=? ORDER BY full_name',
            (branch_id,)
        ).fetchall()

    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    conn.close()
    return render_template('pos.html',
        products=products,
        customers=customers,
        categories=categories
    )

@app.route('/pos/checkout', methods=['POST'])
@login_required
def checkout():
    data        = request.get_json()
    cart        = data.get('cart', [])
    payment     = data.get('payment_type', 'cash')
    paid_amount = float(data.get('paid_amount', 0))
    customer_id = data.get('customer_id') or None
    notes       = data.get('notes', '')

    if not cart:
        return jsonify({'success': False, 'message': 'Cart is empty!'})

    conn      = get_db()
    branch_id = session.get('branch_id') or 1

    # Calculate total
    total = sum(item['price'] * item['qty'] for item in cart)
    change = max(0, paid_amount - total)

    # Check stock availability
    for item in cart:
        prod = conn.execute(
            'SELECT stock_qty, name FROM products WHERE id=?',
            (item['id'],)
        ).fetchone()
        if not prod or prod['stock_qty'] < item['qty']:
            conn.close()
            return jsonify({
                'success': False,
                'message': f'Insufficient stock for {prod["name"] if prod else "product"}!'
            })

    # For credit/debit sales
    if payment == 'credit':
        if not customer_id:
            conn.close()
            return jsonify({
                'success': False,
                'message': 'Please select a customer for credit sales!'
            })
        # Check debit limit
        cust = conn.execute(
            'SELECT * FROM customers WHERE id=?',
            (customer_id,)
        ).fetchone()
        new_balance = cust['debit_balance'] + total
        if cust['debit_limit'] > 0 and new_balance > cust['debit_limit']:
            conn.close()
            return jsonify({
                'success': False,
                'message': f'Customer debit limit exceeded! Limit: TZS {cust["debit_limit"]:,.0f}'
            })
        paid_amount = 0

    # Create sale record
    receipt_no = generate_receipt_no()
    conn.execute('''
        INSERT INTO sales
        (receipt_no,branch_id,user_id,customer_id,
         total_amount,paid_amount,change_amount,payment_type,notes)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', (
        receipt_no, branch_id, session['user_id'],
        customer_id, total, paid_amount, change,
        payment, notes
    ))
    sale_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    # Insert sale items + deduct stock
    for item in cart:
        conn.execute('''
            INSERT INTO sale_items
            (sale_id,product_id,quantity,unit_price,total_price)
            VALUES (?,?,?,?,?)
        ''', (
            sale_id, item['id'], item['qty'],
            item['price'], item['price'] * item['qty']
        ))
        conn.execute(
            'UPDATE products SET stock_qty=stock_qty-? WHERE id=?',
            (item['qty'], item['id'])
        )

    # Update customer debit balance
    if payment == 'credit' and customer_id:
        conn.execute('''
            UPDATE customers
            SET debit_balance=debit_balance+?
            WHERE id=?
        ''', (total, customer_id))

    conn.commit()

    # Build receipt data
    items_data = [{
        'name': conn.execute(
            'SELECT name FROM products WHERE id=?',
            (item['id'],)
        ).fetchone()['name'],
        'qty':   item['qty'],
        'price': item['price'],
        'total': item['price'] * item['qty']
    } for item in cart]

    conn.close()
    return jsonify({
        'success':    True,
        'receipt_no': receipt_no,
        'total':      total,
        'paid':       paid_amount,
        'change':     change,
        'payment':    payment,
        'items':      items_data,
        'cashier':    session['full_name'],
        'branch':     session['branch_name'],
        'datetime':   datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

# ── Sales history ──────────────────────────────────────
@app.route('/sales')
@login_required
def sales():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    date_from = request.args.get('from', datetime.now().strftime('%Y-%m-%d'))
    date_to   = request.args.get('to',   datetime.now().strftime('%Y-%m-%d'))

    if role == 'admin':
        sale_list = conn.execute('''
            SELECT s.*, u.full_name as cashier,
                   b.name as branch_name,
                   c.full_name as customer_name
            FROM sales s
            LEFT JOIN users u ON s.user_id=u.id
            LEFT JOIN branches b ON s.branch_id=b.id
            LEFT JOIN customers c ON s.customer_id=c.id
            WHERE DATE(s.created_at) BETWEEN ? AND ?
            ORDER BY s.created_at DESC
        ''', (date_from, date_to)).fetchall()
        totals = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) as total,
                   COUNT(*) as count
            FROM sales
            WHERE DATE(created_at) BETWEEN ? AND ?
        ''', (date_from, date_to)).fetchone()
    else:
        sale_list = conn.execute('''
            SELECT s.*, u.full_name as cashier,
                   b.name as branch_name,
                   c.full_name as customer_name
            FROM sales s
            LEFT JOIN users u ON s.user_id=u.id
            LEFT JOIN branches b ON s.branch_id=b.id
            LEFT JOIN customers c ON s.customer_id=c.id
            WHERE s.branch_id=?
              AND DATE(s.created_at) BETWEEN ? AND ?
            ORDER BY s.created_at DESC
        ''', (branch_id, date_from, date_to)).fetchall()
        totals = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) as total,
                   COUNT(*) as count
            FROM sales
            WHERE branch_id=?
              AND DATE(created_at) BETWEEN ? AND ?
        ''', (branch_id, date_from, date_to)).fetchone()

    conn.close()
    return render_template('sales.html',
        sale_list=sale_list,
        totals=totals,
        date_from=date_from,
        date_to=date_to
    )

@app.route('/sales/<int:sid>')
@login_required
def sale_detail(sid):
    conn  = get_db()
    sale  = conn.execute('''
        SELECT s.*, u.full_name as cashier,
               b.name as branch_name,
               c.full_name as customer_name
        FROM sales s
        LEFT JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON s.branch_id=b.id
        LEFT JOIN customers c ON s.customer_id=c.id
        WHERE s.id=?
    ''', (sid,)).fetchone()
    items = conn.execute('''
        SELECT si.*, p.name as product_name, p.unit
        FROM sale_items si
        JOIN products p ON si.product_id=p.id
        WHERE si.sale_id=?
    ''', (sid,)).fetchall()
    conn.close()
    return render_template('sale_detail.html',
                           sale=sale, items=items)
    
    
    # ── Expenses ───────────────────────────────────────────
@app.route('/expenses')
@login_required
def expenses():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    date_from = request.args.get('from', datetime.now().strftime('%Y-%m'))
    month     = date_from

    if role == 'admin':
        exps = conn.execute('''
            SELECT e.*, b.name as branch_name,
                   u.full_name as recorded_by_name
            FROM expenses e
            LEFT JOIN branches b ON e.branch_id=b.id
            LEFT JOIN users u ON e.recorded_by=u.id
            WHERE strftime('%Y-%m', e.created_at)=?
            ORDER BY e.created_at DESC
        ''', (month,)).fetchall()
        total_exp = conn.execute('''
            SELECT COALESCE(SUM(amount),0)
            FROM expenses
            WHERE strftime('%Y-%m',created_at)=?
        ''', (month,)).fetchone()[0]
        by_cat = conn.execute('''
            SELECT category,
                   COALESCE(SUM(amount),0) as total
            FROM expenses
            WHERE strftime('%Y-%m',created_at)=?
            GROUP BY category ORDER BY total DESC
        ''', (month,)).fetchall()
    else:
        exps = conn.execute('''
            SELECT e.*, b.name as branch_name,
                   u.full_name as recorded_by_name
            FROM expenses e
            LEFT JOIN branches b ON e.branch_id=b.id
            LEFT JOIN users u ON e.recorded_by=u.id
            WHERE e.branch_id=?
              AND strftime('%Y-%m',e.created_at)=?
            ORDER BY e.created_at DESC
        ''', (branch_id, month)).fetchall()
        total_exp = conn.execute('''
            SELECT COALESCE(SUM(amount),0)
            FROM expenses
            WHERE branch_id=?
              AND strftime('%Y-%m',created_at)=?
        ''', (branch_id, month)).fetchone()[0]
        by_cat = conn.execute('''
            SELECT category,
                   COALESCE(SUM(amount),0) as total
            FROM expenses
            WHERE branch_id=?
              AND strftime('%Y-%m',created_at)=?
            GROUP BY category ORDER BY total DESC
        ''', (branch_id, month)).fetchall()

    conn.close()
    return render_template('expenses.html',
        exps=exps, total_exp=total_exp,
        by_cat=by_cat, month=month
    )

@app.route('/expenses/add', methods=['POST'])
@login_required
def add_expense():
    conn = get_db()
    conn.execute('''
        INSERT INTO expenses
        (branch_id,category,description,amount,recorded_by)
        VALUES (?,?,?,?,?)
    ''', (
        request.form.get('branch_id', session['branch_id']),
        request.form['category'],
        request.form['description'],
        float(request.form['amount']),
        session['user_id']
    ))
    conn.commit(); conn.close()
    flash('Expense recorded!', 'success')
    return redirect(url_for('expenses'))

@app.route('/expenses/delete/<int:eid>', methods=['POST'])
@login_required
def delete_expense(eid):
    conn = get_db()
    conn.execute('DELETE FROM expenses WHERE id=?', (eid,))
    conn.commit(); conn.close()
    flash('Expense deleted!', 'success')
    return redirect(url_for('expenses'))

# ── Reports ────────────────────────────────────────────
@app.route('/reports')
@login_required
def reports():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    period    = request.args.get('period', 'daily')
    date_from = request.args.get('from', datetime.now().strftime('%Y-%m-%d'))
    date_to   = request.args.get('to',   datetime.now().strftime('%Y-%m-%d'))

    # Build date filter based on period
    today = datetime.now()
    if period == 'daily':
        date_from = date_to = today.strftime('%Y-%m-%d')
    elif period == 'weekly':
        date_from = (today - __import__('datetime').timedelta(days=7)).strftime('%Y-%m-%d')
        date_to   = today.strftime('%Y-%m-%d')
    elif period == 'monthly':
        date_from = today.strftime('%Y-%m-01')
        date_to   = today.strftime('%Y-%m-%d')
    elif period == 'yearly':
        date_from = today.strftime('%Y-01-01')
        date_to   = today.strftime('%Y-%m-%d')

    branch_filter = '' if role == 'admin' else f'AND s.branch_id={branch_id}'
    exp_branch    = '' if role == 'admin' else f'AND branch_id={branch_id}'

    # Total sales
    sales_data = conn.execute(f'''
        SELECT COALESCE(SUM(s.total_amount),0) as revenue,
               COUNT(s.id) as transactions,
               COALESCE(SUM(si.quantity * p.buying_price),0) as cost
        FROM sales s
        LEFT JOIN sale_items si ON si.sale_id=s.id
        LEFT JOIN products p ON si.product_id=p.id
        WHERE DATE(s.created_at) BETWEEN ? AND ?
          {branch_filter}
    ''', (date_from, date_to)).fetchone()

    # Expenses in period
    total_expenses = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0)
        FROM expenses
        WHERE DATE(created_at) BETWEEN ? AND ?
          {exp_branch}
    ''', (date_from, date_to)).fetchone()[0]

    revenue     = sales_data['revenue']
    cost        = sales_data['cost']
    gross_profit= revenue - cost
    net_profit  = gross_profit - total_expenses
    margin      = (gross_profit / revenue * 100) if revenue > 0 else 0

    # Sales by day for chart
    daily_sales = conn.execute(f'''
        SELECT DATE(created_at) as day,
               COALESCE(SUM(total_amount),0) as total,
               COUNT(*) as txns
        FROM sales
        WHERE DATE(created_at) BETWEEN ? AND ?
          {branch_filter}
        GROUP BY DATE(created_at)
        ORDER BY day
    ''', (date_from, date_to)).fetchall()

    # Top products
    top_products = conn.execute(f'''
        SELECT p.name, c.name as cat_name,
               SUM(si.quantity) as qty_sold,
               SUM(si.total_price) as revenue,
               SUM(si.quantity * p.buying_price) as cost,
               SUM(si.total_price) - SUM(si.quantity*p.buying_price) as profit
        FROM sale_items si
        JOIN sales s ON si.sale_id=s.id
        JOIN products p ON si.product_id=p.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE DATE(s.created_at) BETWEEN ? AND ?
          {branch_filter}
        GROUP BY p.id ORDER BY qty_sold DESC LIMIT 10
    ''', (date_from, date_to)).fetchall()

    # Top categories
    top_categories = conn.execute(f'''
        SELECT c.name as cat_name,
               d.name as dept_name,
               SUM(si.quantity) as qty_sold,
               SUM(si.total_price) as revenue
        FROM sale_items si
        JOIN sales s ON si.sale_id=s.id
        JOIN products p ON si.product_id=p.id
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        WHERE DATE(s.created_at) BETWEEN ? AND ?
          {branch_filter}
        GROUP BY c.id ORDER BY revenue DESC LIMIT 8
    ''', (date_from, date_to)).fetchall()

    # Expenses by category
    exp_by_cat = conn.execute(f'''
        SELECT category,
               COALESCE(SUM(amount),0) as total
        FROM expenses
        WHERE DATE(created_at) BETWEEN ? AND ?
          {exp_branch}
        GROUP BY category ORDER BY total DESC
    ''', (date_from, date_to)).fetchall()

    # AI expense advice
    advice      = generate_expense_advice(
        revenue, gross_profit, net_profit,
        total_expenses, margin, exp_by_cat
    )

    conn.close()
    return render_template('reports.html',
        period=period,
        date_from=date_from,
        date_to=date_to,
        revenue=revenue,
        cost=cost,
        gross_profit=gross_profit,
        net_profit=net_profit,
        total_expenses=total_expenses,
        margin=margin,
        daily_sales=daily_sales,
        top_products=top_products,
        top_categories=top_categories,
        exp_by_cat=exp_by_cat,
        advice=advice,
        transactions=sales_data['transactions']
    )

def generate_expense_advice(revenue, gross_profit,
                             net_profit, total_expenses,
                             margin, exp_by_cat):
    """AI-style expense advice based on financial data"""
    advice = []

    if revenue == 0:
        return [{
            'type':    'info',
            'title':   'No sales data yet',
            'message': 'Record some sales to get financial advice.'
        }]

    exp_ratio = (total_expenses / revenue * 100) if revenue > 0 else 0

    # Net profit advice
    if net_profit < 0:
        advice.append({
            'type':    'danger',
            'title':   'Business is running at a loss!',
            'message': f'You are losing TZS {abs(net_profit):,.0f}. '
                       f'Immediately reduce expenses or increase sales prices.'
        })
    elif net_profit < gross_profit * 0.1:
        advice.append({
            'type':    'warning',
            'title':   'Net profit is very low',
            'message': f'Net profit margin is only {(net_profit/revenue*100):.1f}%. '
                       f'Consider reducing operational costs.'
        })
    else:
        advice.append({
            'type':    'success',
            'title':   'Business is profitable!',
            'message': f'Net profit is TZS {net_profit:,.0f} '
                       f'({(net_profit/revenue*100):.1f}% margin). Keep it up!'
        })

    # Expense ratio advice
    if exp_ratio > 40:
        advice.append({
            'type':    'danger',
            'title':   'Expenses are too high',
            'message': f'Expenses are {exp_ratio:.1f}% of revenue. '})


# ── Main ───────────────────────────────────────────────
if __name__ == '__main__':
    print("="*50)
    print("  NODDY STORE POS SYSTEM")
    print("="*50)
    init_db()
    print("\nOpen: http://localhost:5001\n")
    app.run(host='0.0.0.0', port=5001, debug=True)