import random
import string
import datetime as dt
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash, send_file)
from database import init_db, get_db
from auth import verify_user, log_action, hash_password

app = Flask(__name__)
app.secret_key = 'noddystore_secret_2024'
app.jinja_env.globals['enumerate'] = enumerate

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def generate_receipt_no():
    chars = string.ascii_uppercase + string.digits
    rand  = ''.join(random.choices(chars, k=6))
    return f'RCP-{datetime.now().strftime("%Y%m%d")}-{rand}'

def get_bf(role, branch_id, alias='s'):
    return '' if role == 'admin' else f'AND {alias}.branch_id={branch_id}'

def get_bf2(role, branch_id, col='branch_id'):
    return '' if role == 'admin' else f'AND {col}={branch_id}'

# ── Auth ───────────────────────────────────────────────
@app.route('/login', methods=['GET','POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        user = verify_user(
            request.form.get('username','').strip(),
            request.form.get('password','').strip()
        )
        if user:
            session.update({
                'user_id':     user['id'],
                'username':    user['username'],
                'full_name':   user['full_name'],
                'role':        user['role'],
                'branch_id':   user['branch_id'],
                'branch_name': user['branch_name'] or 'All Branches'
            })
            log_action(user['id'], user['username'], 'login')
            return redirect(url_for('dashboard'))
        error = 'Invalid username or password. Please try again.'
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
    role = session.get('role')
    today = datetime.now().strftime('%Y-%m-%d')

    # Filters
    bf = get_bf(role, branch_id, 's')
    bf2 = get_bf2(role, branch_id)
    bfp = get_bf2(role, branch_id, 'branch_id')          # For queries without alias
    bfp_products = get_bf2(role, branch_id, 'p.branch_id')  # For queries using alias p

    # Sales summary
    sales_today = conn.execute(f'''
        SELECT
            COALESCE(SUM(s.total_amount),0) as revenue,
            COUNT(s.id) as txns,
            COALESCE(SUM(si.quantity * p.buying_price),0) as cost
        FROM sales s
        LEFT JOIN sale_items si ON si.sale_id = s.id
        LEFT JOIN products p ON si.product_id = p.id
        WHERE DATE(s.created_at)=? {bf}
    ''', (today,)).fetchone()

    # Expenses
    expenses_today = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0)
        FROM expenses
        WHERE DATE(created_at)=? {bf2}
    ''', (today,)).fetchone()[0]

    revenue_today = sales_today['revenue']
    cost_today = sales_today['cost']
    gross_today = revenue_today - cost_today
    net_today = gross_today - expenses_today

    # Statistics
    total_products = conn.execute(f'''
        SELECT COUNT(*)
        FROM products
        WHERE active=1 {bfp}
    ''').fetchone()[0]

    total_customers = conn.execute(f'''
        SELECT COUNT(*)
        FROM customers
        WHERE 1=1 {bf2}
    ''').fetchone()[0]

    total_staff = conn.execute(f'''
        SELECT COUNT(*)
        FROM users
        WHERE active=1
        AND role != "admin"
        {bf2}
    ''').fetchone()[0]

    debit_customers = conn.execute(f'''
        SELECT COUNT(*)
        FROM customers
        WHERE debit_balance > 0
        {bf2}
    ''').fetchone()[0]

    # Low stock products
    low_stock_items = conn.execute(f'''
        SELECT
            p.name,
            p.stock_qty,
            p.min_stock,
            p.unit,
            c.name AS cat_name,
            b.name AS branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id = c.id
        LEFT JOIN branches b ON p.branch_id = b.id
        WHERE p.stock_qty <= p.min_stock
        AND p.active = 1
        {bfp_products}
        ORDER BY p.stock_qty ASC
        LIMIT 10
    ''').fetchall()

    # Branches
    if role == 'admin':
        branches = conn.execute(
            'SELECT * FROM branches'
        ).fetchall()
    else:
        branches = conn.execute(
            'SELECT * FROM branches WHERE id=?',
            (branch_id,)
        ).fetchall()

    # Recent sales
    recent_sales = conn.execute(f'''
        SELECT
            s.*,
            u.full_name AS cashier,
            b.name AS branch_name
        FROM sales s
        LEFT JOIN users u ON s.user_id = u.id
        LEFT JOIN branches b ON s.branch_id = b.id
        WHERE 1=1 {bf}
        ORDER BY s.created_at DESC
        LIMIT 8
    ''').fetchall()

    # Mobile money summary
    try:
        mm_today = conn.execute(f'''
            SELECT
                COALESCE(SUM(amount),0) AS total,
                COUNT(*) AS count
            FROM mobile_money_transactions
            WHERE DATE(created_at)=?
            AND type='credit'
            {bf2}
        ''', (today,)).fetchone()
    except Exception:
        mm_today = {
            'total': 0,
            'count': 0
        }

    conn.close()

    return render_template(
        'dashboard.html',
        revenue_today=revenue_today,
        cost_today=cost_today,
        gross_today=gross_today,
        net_today=net_today,
        expenses_today=expenses_today,
        txns_today=sales_today['txns'],
        total_products=total_products,
        total_customers=total_customers,
        total_staff=total_staff,
        debit_customers=debit_customers,
        low_stock_items=low_stock_items,
        low_stock_count=len(low_stock_items),
        branches=branches,
        recent_sales=recent_sales,
        mm_today=mm_today,
        today=today
    )

# ── Branches ───────────────────────────────────────────
@app.route('/branches')
@login_required
def branches():
    conn     = get_db()
    branches = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
    conn.close()
    return render_template('branches.html', branches=branches)

@app.route('/branches/add', methods=['POST'])
@login_required
def add_branch():
    conn = get_db()
    conn.execute('INSERT INTO branches (name,location,phone) VALUES (?,?,?)',
                 (request.form['name'], request.form.get('location',''), request.form.get('phone','')))
    conn.commit(); conn.close()
    flash('Branch added!', 'success')
    return redirect(url_for('branches'))

@app.route('/branches/delete/<int:bid>', methods=['POST'])
@login_required
def delete_branch(bid):
    conn = get_db()
    conn.execute('DELETE FROM branches WHERE id=?', (bid,))
    conn.commit(); conn.close()
    flash('Branch deleted!', 'success')
    return redirect(url_for('branches'))

# ── Users ──────────────────────────────────────────────
@app.route('/users')
@login_required
def users():
    conn     = get_db()
    users    = conn.execute('''
        SELECT u.*,b.name as branch_name FROM users u
        LEFT JOIN branches b ON u.branch_id=b.id ORDER BY u.full_name
    ''').fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('users.html', users=users, branches=branches)

@app.route('/users/add', methods=['POST'])
@login_required
def add_user():
    conn = get_db()
    try:
        conn.execute('''INSERT INTO users (full_name,username,password,role,branch_id)
                        VALUES (?,?,?,?,?)''',
                     (request.form['full_name'], request.form['username'],
                      hash_password(request.form['password']),
                      request.form['role'], request.form['branch_id']))
        conn.commit()
        flash(f'User "{request.form["username"]}" added successfully! They can now log in.', 'success')
    except:
        flash('Error: Username already exists!', 'error')
    conn.close()
    return redirect(url_for('users'))

@app.route('/users/edit/<int:uid>', methods=['POST'])
@login_required
def edit_user(uid):
    conn = get_db()
    try:
        conn.execute('''UPDATE users SET full_name=?,username=?,role=?,branch_id=?
                        WHERE id=?''',
                     (request.form['full_name'], request.form['username'],
                      request.form['role'], request.form['branch_id'], uid))
        conn.commit()
        flash('User updated successfully!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    conn.close()
    return redirect(url_for('users'))

@app.route('/users/change_password/<int:uid>', methods=['POST'])
@login_required
def change_password(uid):
    conn        = get_db()
    new_pass    = request.form.get('new_password','').strip()
    confirm     = request.form.get('confirm_password','').strip()
    if not new_pass:
        flash('Password cannot be empty!', 'error')
        conn.close()
        return redirect(url_for('users'))
    if new_pass != confirm:
        flash('Passwords do not match!', 'error')
        conn.close()
        return redirect(url_for('users'))
    if len(new_pass) < 4:
        flash('Password must be at least 4 characters!', 'error')
        conn.close()
        return redirect(url_for('users'))
    conn.execute('UPDATE users SET password=? WHERE id=?', (hash_password(new_pass), uid))
    conn.commit()
    user = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    flash(f'Password changed for {user["username"]}!', 'success')
    return redirect(url_for('users'))

@app.route('/users/delete/<int:uid>', methods=['POST'])
@login_required
def delete_user(uid):
    if uid == session.get('user_id'):
        flash('You cannot delete your own account!', 'error')
        return redirect(url_for('users'))
    conn = get_db()
    user = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    if user and user['username'] == 'admin':
        flash('Cannot delete the default admin account!', 'error')
        conn.close()
        return redirect(url_for('users'))
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    conn.commit(); conn.close()
    flash('User deleted!', 'success')
    return redirect(url_for('users'))

@app.route('/users/toggle/<int:uid>', methods=['POST'])
@login_required
def toggle_user(uid):
    conn = get_db()
    conn.execute('UPDATE users SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?', (uid,))
    conn.commit(); conn.close()
    flash('User status updated!', 'success')
    return redirect(url_for('users'))

# ── My Profile (change own password) ──────────────────
@app.route('/profile/change_password', methods=['POST'])
@login_required
def change_own_password():
    conn        = get_db()
    current     = request.form.get('current_password','').strip()
    new_pass    = request.form.get('new_password','').strip()
    confirm     = request.form.get('confirm_password','').strip()
    user = conn.execute('SELECT password FROM users WHERE id=?',
                        (session['user_id'],)).fetchone()
    if user['password'] != hash_password(current):
        flash('Current password is incorrect!', 'error')
        conn.close()
        return redirect(url_for('users'))
    if new_pass != confirm:
        flash('New passwords do not match!', 'error')
        conn.close()
        return redirect(url_for('users'))
    if len(new_pass) < 4:
        flash('Password must be at least 4 characters!', 'error')
        conn.close()
        return redirect(url_for('users'))
    conn.execute('UPDATE users SET password=? WHERE id=?',
                 (hash_password(new_pass), session['user_id']))
    conn.commit(); conn.close()
    flash('Your password has been changed!', 'success')
    return redirect(url_for('users'))

# ── Departments ────────────────────────────────────────
@app.route('/departments')
@login_required
def departments():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    if role == 'admin':
        depts = conn.execute('''
            SELECT d.*,b.name as branch_name,COUNT(c.id) as cat_count
            FROM departments d LEFT JOIN branches b ON d.branch_id=b.id
            LEFT JOIN categories c ON c.department_id=d.id
            GROUP BY d.id ORDER BY d.name
        ''').fetchall()
    else:
        depts = conn.execute('''
            SELECT d.*,b.name as branch_name,COUNT(c.id) as cat_count
            FROM departments d LEFT JOIN branches b ON d.branch_id=b.id
            LEFT JOIN categories c ON c.department_id=d.id
            WHERE d.branch_id=? GROUP BY d.id ORDER BY d.name
        ''', (branch_id,)).fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('departments.html', depts=depts, branches=branches)

@app.route('/departments/add', methods=['POST'])
@login_required
def add_department():
    conn = get_db()
    conn.execute('INSERT INTO departments (name,description,branch_id) VALUES (?,?,?)',
                 (request.form['name'], request.form.get('description',''), request.form['branch_id']))
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
        cats  = conn.execute('''
            SELECT c.*,d.name as dept_name,COUNT(p.id) as product_count
            FROM categories c LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN products p ON p.category_id=c.id AND p.active=1
            GROUP BY c.id ORDER BY c.name
        ''').fetchall()
        depts = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    else:
        cats  = conn.execute('''
            SELECT c.*,d.name as dept_name,COUNT(p.id) as product_count
            FROM categories c LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN products p ON p.category_id=c.id AND p.active=1
            WHERE d.branch_id=? GROUP BY c.id ORDER BY c.name
        ''', (branch_id,)).fetchall()
        depts = conn.execute('SELECT * FROM departments WHERE branch_id=?', (branch_id,)).fetchall()
    conn.close()
    return render_template('categories.html', cats=cats, depts=depts)

@app.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    conn = get_db()
    conn.execute('INSERT INTO categories (name,department_id,description) VALUES (?,?,?)',
                 (request.form['name'], request.form['department_id'], request.form.get('description','')))
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
    stock_filter = request.args.get('stock','')

    query  = '''SELECT p.*,c.name as cat_name,d.name as dept_name,b.name as branch_name,
                       CASE WHEN p.stock_qty<=0 THEN 'out'
                            WHEN p.stock_qty<=p.min_stock THEN 'low'
                            ELSE 'ok' END as stock_status
                FROM products p
                LEFT JOIN categories c ON p.category_id=c.id
                LEFT JOIN departments d ON c.department_id=d.id
                LEFT JOIN branches b ON p.branch_id=b.id
                WHERE p.active=1'''
    params = []
    if role != 'admin':
        query += ' AND p.branch_id=?'; params.append(branch_id)
    if search:
        query += ' AND (p.name LIKE ? OR p.barcode LIKE ?)'; params += [f'%{search}%', f'%{search}%']
    if cat_id:
        query += ' AND p.category_id=?'; params.append(cat_id)
    if dept_id:
        query += ' AND d.id=?'; params.append(dept_id)
    if stock_filter == 'low':
        query += ' AND p.stock_qty<=p.min_stock AND p.stock_qty>0'
    elif stock_filter == 'out':
        query += ' AND p.stock_qty<=0'
    query += ' ORDER BY p.name'

    prods    = conn.execute(query, params).fetchall()
    cats     = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    depts    = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()

    # Stock summary
    bfp = get_bf2(role, branch_id, 'p.branch_id')
    stock_summary = conn.execute(f'''
        SELECT COUNT(*) as total,
               SUM(CASE WHEN stock_qty<=0 THEN 1 ELSE 0 END) as out_of_stock,
               SUM(CASE WHEN stock_qty>0 AND stock_qty<=min_stock THEN 1 ELSE 0 END) as low_stock,
               SUM(CASE WHEN stock_qty>min_stock THEN 1 ELSE 0 END) as ok_stock
        FROM products WHERE active=1 {bfp}
    ''').fetchone()

    conn.close()
    return render_template('products.html',
        prods=prods, cats=cats, depts=depts,
        branches=branches, search=search, cat_id=cat_id,
        dept_id=dept_id, stock_filter=stock_filter,
        stock_summary=stock_summary)

@app.route('/products/add', methods=['POST'])
@login_required
def add_product():
    conn = get_db()
    try:
        conn.execute('''INSERT INTO products
            (name,barcode,category_id,branch_id,buying_price,
             selling_price,stock_qty,min_stock,unit,description)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (request.form['name'], request.form.get('barcode') or None,
             request.form['category_id'], request.form['branch_id'],
             float(request.form.get('buying_price',0)),
             float(request.form.get('selling_price',0)),
             int(request.form.get('stock_qty',0)),
             int(request.form.get('min_stock',5)),
             request.form.get('unit','pcs'),
             request.form.get('description','')))
        conn.commit(); flash('Product added!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    conn.close()
    return redirect(url_for('products'))

@app.route('/products/edit/<int:pid>', methods=['GET','POST'])
@login_required
def edit_product(pid):
    conn = get_db()
    if request.method == 'POST':
        conn.execute('''UPDATE products SET
            name=?,barcode=?,category_id=?,branch_id=?,
            buying_price=?,selling_price=?,stock_qty=?,
            min_stock=?,unit=?,description=? WHERE id=?''',
            (request.form['name'], request.form.get('barcode') or None,
             request.form['category_id'], request.form['branch_id'],
             float(request.form.get('buying_price',0)),
             float(request.form.get('selling_price',0)),
             int(request.form.get('stock_qty',0)),
             int(request.form.get('min_stock',5)),
             request.form.get('unit','pcs'),
             request.form.get('description',''), pid))
        conn.commit(); conn.close()
        flash('Product updated!', 'success')
        return redirect(url_for('products'))
    prod     = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    cats     = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    depts    = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('edit_product.html', prod=prod, cats=cats, depts=depts, branches=branches)

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
    bfp       = get_bf2(role, branch_id, 'p.branch_id')
    bfs       = get_bf(role, branch_id, 's')

    low_stock = conn.execute(f'''
        SELECT p.*,c.name as cat_name,d.name as dept_name,b.name as branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.stock_qty<=p.min_stock AND p.active=1 {bfp}
        ORDER BY p.stock_qty ASC
    ''').fetchall()

    all_stock = conn.execute(f'''
        SELECT p.*,c.name as cat_name,d.name as dept_name,b.name as branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.active=1 {bfp} ORDER BY p.name
    ''').fetchall()

    top_selling = conn.execute(f'''
        SELECT p.name,c.name as cat_name,
               SUM(si.quantity) as total_sold,
               SUM(si.total_price) as total_revenue
        FROM sale_items si
        JOIN products p ON si.product_id=p.id
        JOIN sales s ON si.sale_id=s.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE 1=1 {bfs}
        GROUP BY p.id ORDER BY total_sold DESC LIMIT 10
    ''').fetchall()

    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('stock.html',
        low_stock=low_stock, all_stock=all_stock,
        top_selling=top_selling, branches=branches)

@app.route('/stock/adjust/<int:pid>', methods=['POST'])
@login_required
def adjust_stock(pid):
    conn = get_db()
    qty  = int(request.form.get('qty',0))
    mode = request.form.get('mode','add')
    if mode == 'add':
        conn.execute('UPDATE products SET stock_qty=stock_qty+? WHERE id=?', (qty,pid))
    else:
        conn.execute('UPDATE products SET stock_qty=MAX(0,stock_qty-?) WHERE id=?', (qty,pid))
    conn.commit(); conn.close()
    flash('Stock updated!', 'success')
    return redirect(url_for('stock'))

@app.route('/stock/transfer', methods=['POST'])
@login_required
def transfer_stock():
    conn   = get_db()
    pid    = int(request.form['product_id'])
    from_b = int(request.form['from_branch'])
    to_b   = int(request.form['to_branch'])
    qty    = int(request.form['quantity'])
    conn.execute('UPDATE products SET stock_qty=MAX(0,stock_qty-?) WHERE id=? AND branch_id=?',
                 (qty,pid,from_b))
    dest = conn.execute('''SELECT id FROM products
                           WHERE name=(SELECT name FROM products WHERE id=?) AND branch_id=?''',
                        (pid,to_b)).fetchone()
    if dest:
        conn.execute('UPDATE products SET stock_qty=stock_qty+? WHERE id=?', (qty,dest['id']))
    conn.execute('''INSERT INTO stock_transfers (product_id,from_branch,to_branch,quantity,transferred_by)
                    VALUES (?,?,?,?,?)''', (pid,from_b,to_b,qty,session['user_id']))
    conn.commit(); conn.close()
    flash(f'Transferred {qty} units!', 'success')
    return redirect(url_for('stock'))

# ── Customers ──────────────────────────────────────────
@app.route('/customers')
@login_required
def customers():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    bf2       = get_bf2(role, branch_id, 'c.branch_id')
    custs     = conn.execute(f'''
        SELECT c.*,b.name as branch_name FROM customers c
        LEFT JOIN branches b ON c.branch_id=b.id
        WHERE 1=1 {bf2} ORDER BY c.full_name
    ''').fetchall()
    branches  = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('customers.html', custs=custs, branches=branches)

@app.route('/customers/add', methods=['POST'])
@login_required
def add_customer():
    conn = get_db()
    conn.execute('''INSERT INTO customers (full_name,phone,email,address,debit_limit,branch_id)
                    VALUES (?,?,?,?,?,?)''',
                 (request.form['full_name'], request.form.get('phone',''),
                  request.form.get('email',''), request.form.get('address',''),
                  float(request.form.get('debit_limit',0)), request.form['branch_id']))
    conn.commit(); conn.close()
    flash('Customer added!', 'success')
    return redirect(url_for('customers'))

@app.route('/customers/pay/<int:cid>', methods=['POST'])
@login_required
def customer_pay(cid):
    conn   = get_db()
    amount = float(request.form.get('amount',0))
    conn.execute('UPDATE customers SET debit_balance=MAX(0,debit_balance-?) WHERE id=?', (amount,cid))
    conn.commit(); conn.close()
    flash(f'Payment of TZS {amount:,.0f} recorded!', 'success')
    return redirect(url_for('customers'))

# ── POS ────────────────────────────────────────────────
@app.route('/pos')
@login_required
def pos():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    bfp       = get_bf2(role, branch_id, 'p.branch_id')
    bfc       = get_bf2(role, branch_id, 'c.branch_id')
    products  = conn.execute(f'''
        SELECT p.*,c.name as cat_name,d.name as dept_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        WHERE p.active=1 AND p.stock_qty>0 {bfp} ORDER BY p.name
    ''').fetchall()
    customers  = conn.execute(f'''
        SELECT * FROM customers c WHERE 1=1 {bfc} ORDER BY full_name
    ''').fetchall()
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    conn.close()
    return render_template('pos.html', products=products,
                           customers=customers, categories=categories)

@app.route('/pos/checkout', methods=['POST'])
@login_required
def checkout():
    data        = request.get_json()
    cart        = data.get('cart',[])
    payment     = data.get('payment_type','cash')
    paid_amount = float(data.get('paid_amount',0))
    customer_id = data.get('customer_id') or None
    notes       = data.get('notes','')
    if not cart:
        return jsonify({'success':False,'message':'Cart is empty!'})
    conn      = get_db()
    branch_id = session.get('branch_id') or 1
    total     = sum(i['price']*i['qty'] for i in cart)
    change    = max(0, paid_amount-total)

    for item in cart:
        prod = conn.execute('SELECT stock_qty,name FROM products WHERE id=?', (item['id'],)).fetchone()
        if not prod or prod['stock_qty'] < item['qty']:
            conn.close()
            return jsonify({'success':False,
                'message':f'Insufficient stock for {prod["name"] if prod else "product"}!'})

    if payment == 'credit':
        if not customer_id:
            conn.close()
            return jsonify({'success':False,'message':'Select a customer for credit sale!'})
        cust = conn.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
        if cust['debit_limit'] > 0 and (cust['debit_balance']+total) > cust['debit_limit']:
            conn.close()
            return jsonify({'success':False,
                'message':f'Debit limit exceeded! Limit: TZS {cust["debit_limit"]:,.0f}'})
        paid_amount = 0

    receipt_no = generate_receipt_no()
    conn.execute('''INSERT INTO sales
        (receipt_no,branch_id,user_id,customer_id,
         total_amount,paid_amount,change_amount,payment_type,notes)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (receipt_no,branch_id,session['user_id'],
         customer_id,total,paid_amount,change,payment,notes))
    sale_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    items_data = []
    for item in cart:
        conn.execute('''INSERT INTO sale_items (sale_id,product_id,quantity,unit_price,total_price)
                        VALUES (?,?,?,?,?)''',
                     (sale_id,item['id'],item['qty'],item['price'],item['price']*item['qty']))
        conn.execute('UPDATE products SET stock_qty=stock_qty-? WHERE id=?', (item['qty'],item['id']))
        pname = conn.execute('SELECT name FROM products WHERE id=?', (item['id'],)).fetchone()['name']
        items_data.append({'name':pname,'qty':item['qty'],
                           'price':item['price'],'total':item['price']*item['qty']})

    if payment == 'credit' and customer_id:
        conn.execute('UPDATE customers SET debit_balance=debit_balance+? WHERE id=?', (total,customer_id))

    conn.commit(); conn.close()
    return jsonify({'success':True,'receipt_no':receipt_no,
                    'total':total,'paid':paid_amount,'change':change,
                    'payment':payment,'items':items_data,
                    'cashier':session['full_name'],
                    'branch':session['branch_name'],
                    'datetime':datetime.now().strftime('%Y-%m-%d %H:%M:%S')})

# ── Sales ──────────────────────────────────────────────
@app.route('/sales')
@login_required
def sales():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    date_from = request.args.get('from', datetime.now().strftime('%Y-%m-%d'))
    date_to   = request.args.get('to',   datetime.now().strftime('%Y-%m-%d'))
    bf        = get_bf(role, branch_id, 's')

    sale_list = conn.execute(f'''
        SELECT s.*,u.full_name as cashier,b.name as branch_name,
               c.full_name as customer_name
        FROM sales s
        LEFT JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON s.branch_id=b.id
        LEFT JOIN customers c ON s.customer_id=c.id
        WHERE DATE(s.created_at) BETWEEN ? AND ? {bf}
        ORDER BY s.created_at DESC
    ''', (date_from,date_to)).fetchall()

    totals = conn.execute(f'''
        SELECT COALESCE(SUM(total_amount),0) as total,COUNT(*) as count
        FROM sales s WHERE DATE(created_at) BETWEEN ? AND ? {bf}
    ''', (date_from,date_to)).fetchone()
    conn.close()
    return render_template('sales.html',
        sale_list=sale_list, totals=totals,
        date_from=date_from, date_to=date_to)

@app.route('/sales/<int:sid>')
@login_required
def sale_detail(sid):
    conn  = get_db()
    sale  = conn.execute('''
        SELECT s.*,u.full_name as cashier,b.name as branch_name,
               c.full_name as customer_name
        FROM sales s
        LEFT JOIN users u ON s.user_id=u.id
        LEFT JOIN branches b ON s.branch_id=b.id
        LEFT JOIN customers c ON s.customer_id=c.id
        WHERE s.id=?
    ''', (sid,)).fetchone()
    items = conn.execute('''
        SELECT si.*,p.name as product_name,p.unit
        FROM sale_items si JOIN products p ON si.product_id=p.id
        WHERE si.sale_id=?
    ''', (sid,)).fetchall()
    conn.close()
    return render_template('sale_detail.html', sale=sale, items=items)

# ── Expenses ───────────────────────────────────────────
@app.route('/expenses')
@login_required
def expenses():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    month     = request.args.get('from', datetime.now().strftime('%Y-%m'))
    bf        = get_bf2(role, branch_id, 'e.branch_id')
    bf2       = get_bf2(role, branch_id)

    exps = conn.execute(f'''
        SELECT e.*,b.name as branch_name,u.full_name as recorded_by_name
        FROM expenses e
        LEFT JOIN branches b ON e.branch_id=b.id
        LEFT JOIN users u ON e.recorded_by=u.id
        WHERE strftime('%Y-%m',e.created_at)=? {bf}
        ORDER BY e.created_at DESC
    ''', (month,)).fetchall()

    total_exp = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0) FROM expenses
        WHERE strftime('%Y-%m',created_at)=? {bf2}
    ''', (month,)).fetchone()[0]

    by_cat = conn.execute(f'''
        SELECT category,COALESCE(SUM(amount),0) as total
        FROM expenses WHERE strftime('%Y-%m',created_at)=? {bf2}
        GROUP BY category ORDER BY total DESC
    ''', (month,)).fetchall()

    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('expenses.html',
        exps=exps, total_exp=total_exp,
        by_cat=by_cat, month=month, branches=branches)

@app.route('/expenses/add', methods=['POST'])
@login_required
def add_expense():
    conn = get_db()
    conn.execute('''INSERT INTO expenses (branch_id,category,description,amount,recorded_by)
                    VALUES (?,?,?,?,?)''',
                 (request.form.get('branch_id', session['branch_id']),
                  request.form['category'], request.form['description'],
                  float(request.form['amount']), session['user_id']))
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
    period    = request.args.get('period','daily')
    today     = datetime.now()

    if period == 'daily':
        date_from = date_to = today.strftime('%Y-%m-%d')
    elif period == 'weekly':
        date_from = (today - dt.timedelta(days=7)).strftime('%Y-%m-%d')
        date_to   = today.strftime('%Y-%m-%d')
    elif period == 'monthly':
        date_from = today.strftime('%Y-%m-01')
        date_to   = today.strftime('%Y-%m-%d')
    elif period == 'yearly':
        date_from = today.strftime('%Y-01-01')
        date_to   = today.strftime('%Y-%m-%d')
    else:
        date_from = request.args.get('from', today.strftime('%Y-%m-%d'))
        date_to   = request.args.get('to',   today.strftime('%Y-%m-%d'))

    bf  = get_bf(role, branch_id, 's')
    bf2 = get_bf2(role, branch_id)

    sales_data = conn.execute(f'''
        SELECT COALESCE(SUM(s.total_amount),0) as revenue,
               COUNT(s.id) as transactions,
               COALESCE(SUM(si.quantity*p.buying_price),0) as cost
        FROM sales s
        LEFT JOIN sale_items si ON si.sale_id=s.id
        LEFT JOIN products p ON si.product_id=p.id
        WHERE DATE(s.created_at) BETWEEN ? AND ? {bf}
    ''', (date_from,date_to)).fetchone()

    total_expenses = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0) FROM expenses
        WHERE DATE(created_at) BETWEEN ? AND ? {bf2}
    ''', (date_from,date_to)).fetchone()[0]

    revenue      = sales_data['revenue']
    cost         = sales_data['cost']
    gross_profit = revenue - cost
    net_profit   = gross_profit - total_expenses
    margin       = (gross_profit/revenue*100) if revenue > 0 else 0

    daily_sales = conn.execute(f'''
        SELECT DATE(created_at) as day,
               COALESCE(SUM(total_amount),0) as total,
               COUNT(*) as txns
        FROM sales s WHERE DATE(created_at) BETWEEN ? AND ? {bf}
        GROUP BY DATE(created_at) ORDER BY day
    ''', (date_from,date_to)).fetchall()

    top_products = conn.execute(f'''
        SELECT p.name,c.name as cat_name,
               SUM(si.quantity) as qty_sold,
               SUM(si.total_price) as revenue,
               SUM(si.quantity*p.buying_price) as cost,
               SUM(si.total_price)-SUM(si.quantity*p.buying_price) as profit
        FROM sale_items si
        JOIN sales s ON si.sale_id=s.id
        JOIN products p ON si.product_id=p.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE DATE(s.created_at) BETWEEN ? AND ? {bf}
        GROUP BY p.id ORDER BY qty_sold DESC LIMIT 10
    ''', (date_from,date_to)).fetchall()

    top_categories = conn.execute(f'''
        SELECT c.name as cat_name,d.name as dept_name,
               SUM(si.quantity) as qty_sold,
               SUM(si.total_price) as revenue
        FROM sale_items si
        JOIN sales s ON si.sale_id=s.id
        JOIN products p ON si.product_id=p.id
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        WHERE DATE(s.created_at) BETWEEN ? AND ? {bf}
        GROUP BY c.id ORDER BY revenue DESC LIMIT 8
    ''', (date_from,date_to)).fetchall()

    exp_by_cat = conn.execute(f'''
        SELECT category,COALESCE(SUM(amount),0) as total
        FROM expenses WHERE DATE(created_at) BETWEEN ? AND ? {bf2}
        GROUP BY category ORDER BY total DESC
    ''', (date_from,date_to)).fetchall()

    advice = generate_expense_advice(revenue, gross_profit, net_profit,
                                      total_expenses, margin, exp_by_cat)
    conn.close()
    return render_template('reports.html',
        period=period, date_from=date_from, date_to=date_to,
        revenue=revenue, cost=cost, gross_profit=gross_profit,
        net_profit=net_profit, total_expenses=total_expenses, margin=margin,
        daily_sales=daily_sales, top_products=top_products,
        top_categories=top_categories, exp_by_cat=exp_by_cat,
        advice=advice, transactions=sales_data['transactions'])

def generate_expense_advice(revenue, gross_profit, net_profit,
                             total_expenses, margin, exp_by_cat):
    advice = []
    if revenue == 0:
        return [{'type':'info','title':'No sales data yet',
                 'message':'Record some sales to get financial advice.'}]
    exp_ratio = (total_expenses/revenue*100) if revenue > 0 else 0
    if net_profit < 0:
        advice.append({'type':'danger','title':'Business is running at a loss!',
            'message':f'Losing TZS {abs(net_profit):,.0f}. Reduce expenses or increase prices immediately.'})
    elif net_profit < gross_profit*0.1:
        advice.append({'type':'warning','title':'Net profit is very low',
            'message':f'Net margin only {(net_profit/revenue*100):.1f}%. Reduce operational costs.'})
    else:
        advice.append({'type':'success','title':'Business is profitable!',
            'message':f'Net profit TZS {net_profit:,.0f} ({(net_profit/revenue*100):.1f}% margin).'})
    if exp_ratio > 40:
        advice.append({'type':'danger','title':'Expenses are too high',
            'message':f'Expenses at {exp_ratio:.1f}% of revenue. Target below 30%. Minimize now.'})
    elif exp_ratio > 25:
        advice.append({'type':'warning','title':'Expenses moderate',
            'message':f'Expenses at {exp_ratio:.1f}% of revenue. Look for areas to cut.'})
    else:
        advice.append({'type':'success','title':'Expenses well controlled',
            'message':f'Expenses only {exp_ratio:.1f}% of revenue. Excellent!'})
    if margin < 10:
        advice.append({'type':'danger','title':'Gross margin critically low',
            'message':f'Only {margin:.1f}% gross margin. Review buying or selling prices.'})
    elif margin < 20:
        advice.append({'type':'warning','title':'Gross margin needs improvement',
            'message':f'{margin:.1f}% margin. Negotiate better supplier prices.'})
    else:
        advice.append({'type':'success','title':'Good gross margin',
            'message':f'{margin:.1f}% gross margin is healthy.'})
    if exp_by_cat:
        top = exp_by_cat[0]
        if top['total'] > revenue*0.15:
            advice.append({'type':'warning','title':f'High spending on {top["category"]}',
                'message':f'TZS {top["total"]:,.0f} on {top["category"]} '
                          f'({(top["total"]/revenue*100):.1f}% of revenue). Consider reducing.'})
    return advice

@app.route('/reports/pdf')
@login_required
def report_pdf():
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate,Table,TableStyle,Paragraph,Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        import io

        period    = request.args.get('period','daily')
        date_from = request.args.get('from', datetime.now().strftime('%Y-%m-%d'))
        date_to   = request.args.get('to',   datetime.now().strftime('%Y-%m-%d'))
        conn      = get_db()
        branch_id = session.get('branch_id')
        role      = session.get('role')
        bf        = get_bf(role, branch_id, 's')
        bf2       = get_bf2(role, branch_id)

        sd = conn.execute(f'''
            SELECT COALESCE(SUM(s.total_amount),0) as revenue,
                   COUNT(s.id) as transactions,
                   COALESCE(SUM(si.quantity*p.buying_price),0) as cost
            FROM sales s
            LEFT JOIN sale_items si ON si.sale_id=s.id
            LEFT JOIN products p ON si.product_id=p.id
            WHERE DATE(s.created_at) BETWEEN ? AND ? {bf}
        ''', (date_from,date_to)).fetchone()

        te = conn.execute(f'''
            SELECT COALESCE(SUM(amount),0) FROM expenses
            WHERE DATE(created_at) BETWEEN ? AND ? {bf2}
        ''', (date_from,date_to)).fetchone()[0]

        revenue      = sd['revenue']
        cost         = sd['cost']
        gross_profit = revenue-cost
        net_profit   = gross_profit-te

        tp = conn.execute(f'''
            SELECT p.name,SUM(si.quantity) as qty,
                   SUM(si.total_price) as rev,
                   SUM(si.total_price)-SUM(si.quantity*p.buying_price) as profit
            FROM sale_items si JOIN sales s ON si.sale_id=s.id
            JOIN products p ON si.product_id=p.id
            WHERE DATE(s.created_at) BETWEEN ? AND ? {bf}
            GROUP BY p.id ORDER BY qty DESC LIMIT 15
        ''', (date_from,date_to)).fetchall()

        exp_cats = conn.execute(f'''
            SELECT category,COALESCE(SUM(amount),0) as total
            FROM expenses WHERE DATE(created_at) BETWEEN ? AND ? {bf2}
            GROUP BY category ORDER BY total DESC
        ''', (date_from,date_to)).fetchall()
        conn.close()

        buf  = io.BytesIO()
        doc  = SimpleDocTemplate(buf, pagesize=A4, topMargin=40, bottomMargin=40)
        styl = getSampleStyleSheet()
        els  = []

        els.append(Paragraph('NODDY STORE — FINANCIAL REPORT', styl['Title']))
        els.append(Paragraph(
            f'Period: {date_from} to {date_to} | Branch: {session["branch_name"]} | '
            f'Generated by: {session["full_name"]} | {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            styl['Normal']))
        els.append(Spacer(1,16))

        els.append(Paragraph('Profit & Loss Statement', styl['Heading2']))
        pl = [
            ['Item','Amount (TZS)','Notes'],
            ['Total Revenue', f'{revenue:,.0f}', f'{sd["transactions"]} transactions'],
            ['Cost of Goods Sold', f'{cost:,.0f}',
             f'{(cost/revenue*100 if revenue else 0):.1f}% of revenue'],
            ['Gross Profit', f'{gross_profit:,.0f}',
             f'{(gross_profit/revenue*100 if revenue else 0):.1f}% margin'],
            ['Total Expenses', f'{te:,.0f}',
             f'{(te/revenue*100 if revenue else 0):.1f}% of revenue'],
            ['NET PROFIT / LOSS', f'{net_profit:,.0f}',
             'PROFIT' if net_profit >= 0 else 'LOSS'],
        ]
        t = Table(pl, colWidths=[200,150,130])
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1a1a2e')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('BACKGROUND',(0,5),(-1,5),
             colors.HexColor('#1a4a1a') if net_profit>=0 else colors.HexColor('#4a1a1a')),
            ('TEXTCOLOR',(0,5),(-1,5),colors.white),
            ('FONTNAME',(0,5),(-1,5),'Helvetica-Bold'),
            ('GRID',(0,0),(-1,-1),0.5,colors.grey),
            ('FONTSIZE',(0,0),(-1,-1),11),
            ('ROWBACKGROUNDS',(0,1),(-1,4),[colors.white,colors.HexColor('#f9f9f9')]),
            ('ALIGN',(1,0),(1,-1),'RIGHT'),
        ]))
        els.append(t); els.append(Spacer(1,20))

        if exp_cats:
            els.append(Paragraph('Expenses by Category', styl['Heading2']))
            exp_data = [['Category','Amount (TZS)','% of Revenue']]
            for e in exp_cats:
                pct = (e['total']/revenue*100) if revenue > 0 else 0
                exp_data.append([e['category'], f'{e["total"]:,.0f}', f'{pct:.1f}%'])
            t_exp = Table(exp_data, colWidths=[200,150,130])
            t_exp.setStyle(TableStyle([
                ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#c0392b')),
                ('TEXTCOLOR',(0,0),(-1,0),colors.white),
                ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
                ('GRID',(0,0),(-1,-1),0.5,colors.grey),
                ('FONTSIZE',(0,0),(-1,-1),10),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#fff5f5')]),
                ('ALIGN',(1,0),(2,-1),'RIGHT'),
            ]))
            els.append(t_exp); els.append(Spacer(1,20))

        if tp:
            els.append(Paragraph('Top Products', styl['Heading2']))
            prod_data = [['#','Product','Qty Sold','Revenue (TZS)','Profit (TZS)']]
            for i,p in enumerate(tp,1):
                prod_data.append([str(i), p['name'], str(p['qty']),
                                  f'{p["rev"]:,.0f}', f'{p["profit"]:,.0f}'])
            t2 = Table(prod_data, colWidths=[30,180,70,110,90])
            t2.setStyle(TableStyle([
                ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f39c12')),
                ('TEXTCOLOR',(0,0),(-1,0),colors.black),
                ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
                ('GRID',(0,0),(-1,-1),0.5,colors.grey),
                ('FONTSIZE',(0,0),(-1,-1),10),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#fef9f0')]),
                ('ALIGN',(2,0),(4,-1),'RIGHT'),
            ]))
            els.append(t2)

        doc.build(els)
        buf.seek(0)
        return send_file(buf, mimetype='application/pdf',
                        as_attachment=True,
                        download_name=f'noddy_report_{date_from}_{date_to}.pdf')
    except ImportError:
        flash('Run: pip install reportlab', 'error')
        return redirect(url_for('reports'))
    except Exception as e:
        flash(f'PDF Error: {str(e)}', 'error')
        return redirect(url_for('reports'))

# ── Staff ──────────────────────────────────────────────
@app.route('/staff')
@login_required
def staff():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    bfu       = get_bf2(role, branch_id, 'u.branch_id')
    bf2       = get_bf2(role, branch_id)

    staff_list = conn.execute(f'''
        SELECT u.*,b.name as branch_name,
               COALESCE(s.amount,0) as salary,
               COALESCE(s.paid,0) as salary_paid,
               COUNT(DISTINCT a.id) as attendance_days
        FROM users u
        LEFT JOIN branches b ON u.branch_id=b.id
        LEFT JOIN salaries s ON s.user_id=u.id
            AND s.month=strftime('%Y-%m','now')
        LEFT JOIN attendance a ON a.user_id=u.id
            AND strftime('%Y-%m',a.date)=strftime('%Y-%m','now')
        WHERE u.role!='admin' {bfu}
        GROUP BY u.id ORDER BY u.full_name
    ''').fetchall()

    branches         = conn.execute('SELECT * FROM branches').fetchall()
    recommendation   = get_staff_recommendation(conn, branch_id if role!='admin' else None)
    salary_total     = conn.execute(f'''
        SELECT COALESCE(SUM(amount),0) FROM salaries
        WHERE month=strftime('%Y-%m','now') {bf2}
    ''').fetchone()[0]
    attendance_today = conn.execute(f'''
        SELECT COUNT(*) FROM attendance
        WHERE date=DATE('now') AND status='present' {bf2}
    ''').fetchone()[0]
    conn.close()
    return render_template('staff.html',
        staff_list=staff_list, branches=branches,
        recommendation=recommendation,
        salary_total=salary_total, attendance_today=attendance_today)

def get_staff_recommendation(conn, branch_id=None):
    bf  = f'AND branch_id={branch_id}' if branch_id else ''
    bfu = f'AND users.branch_id={branch_id}' if branch_id else ''
    avg = conn.execute(f'''
        SELECT AVG(c) FROM (
            SELECT COUNT(*) as c FROM sales
            WHERE created_at>=DATE('now','-30 days') {bf}
            GROUP BY DATE(created_at))
    ''').fetchone()[0] or 0
    cnt = conn.execute(f'SELECT COUNT(*) FROM users WHERE active=1 AND role!="admin" {bfu}').fetchone()[0]
    rec = max(1, round(avg/20))
    if cnt < rec:
        status = 'increase'
        msg = f'Based on {avg:.0f} avg daily transactions, you need {rec} staff. Hire {rec-cnt} more.'
    elif cnt > rec+2:
        status = 'reduce'
        msg = f'You have {cnt} staff for {avg:.0f} daily transactions. Reduce by {cnt-rec} to save costs.'
    else:
        status = 'optimal'
        msg = f'Your {cnt} staff is optimal for {avg:.0f} daily transactions.'
    return {'status':status,'message':msg,'current':cnt,'recommended':rec,'avg_txns':round(avg,1)}

@app.route('/staff/attendance', methods=['POST'])
@login_required
def mark_attendance():
    conn = get_db()
    conn.execute('''INSERT OR REPLACE INTO attendance (user_id,branch_id,date,check_in,status)
                    VALUES (?,?,DATE('now'),TIME('now'),?)''',
                 (request.form['user_id'], session['branch_id'], request.form.get('status','present')))
    conn.commit(); conn.close()
    flash('Attendance marked!', 'success')
    return redirect(url_for('staff'))

@app.route('/staff/checkout/<int:uid>', methods=['POST'])
@login_required
def staff_checkout(uid):
    conn = get_db()
    conn.execute("UPDATE attendance SET check_out=TIME('now') WHERE user_id=? AND date=DATE('now')", (uid,))
    conn.commit(); conn.close()
    flash('Check-out recorded!', 'success')
    return redirect(url_for('staff'))

@app.route('/staff/salary/set', methods=['POST'])
@login_required
def set_salary():
    conn = get_db()
    try:
        conn.execute('''INSERT OR REPLACE INTO salaries (user_id,branch_id,amount,month)
                        VALUES (?,?,?,strftime('%Y-%m','now'))''',
                     (request.form['user_id'], session.get('branch_id',1), float(request.form['amount'])))
        conn.commit(); flash('Salary set!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    conn.close()
    return redirect(url_for('staff'))

@app.route('/staff/salary/pay/<int:uid>', methods=['POST'])
@login_required
def pay_salary(uid):
    conn   = get_db()
    stf    = conn.execute('SELECT full_name FROM users WHERE id=?', (uid,)).fetchone()
    salary = conn.execute('''SELECT amount FROM salaries
                             WHERE user_id=? AND month=strftime('%Y-%m','now')''', (uid,)).fetchone()
    conn.execute('''UPDATE salaries SET paid=1,paid_at=CURRENT_TIMESTAMP
                    WHERE user_id=? AND month=strftime('%Y-%m','now')''', (uid,))
    if salary:
        conn.execute('''INSERT INTO expenses (branch_id,category,description,amount,recorded_by)
                        VALUES (?,?,?,?,?)''',
                     (session.get('branch_id',1),'Salaries',
                      f'Salary — {stf["full_name"]}', salary['amount'], session['user_id']))
    conn.commit(); conn.close()
    flash('Salary paid and recorded!', 'success')
    return redirect(url_for('staff'))

@app.route('/staff/attendance/report')
@login_required
def attendance_report():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    month     = request.args.get('month', datetime.now().strftime('%Y-%m'))
    bf        = get_bf2(role, branch_id, 'a.branch_id')
    records   = conn.execute(f'''
        SELECT a.*,u.full_name,b.name as branch_name
        FROM attendance a JOIN users u ON a.user_id=u.id
        LEFT JOIN branches b ON a.branch_id=b.id
        WHERE strftime('%Y-%m',a.date)=? {bf}
        ORDER BY a.date DESC,u.full_name
    ''', (month,)).fetchall()
    conn.close()
    return render_template('attendance.html', records=records, month=month)

@app.route('/staff/performance')
@login_required
def staff_performance():
    conn = get_db()

    branch_id = session.get('branch_id')
    role = session.get('role')
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))

    bf = get_bf(role, branch_id, 's')

    perf = conn.execute(f'''
        SELECT
            u.id,
            u.full_name,
            u.role,
            b.name AS branch_name,
            COUNT(DISTINCT s.id) AS total_sales,
            COALESCE(SUM(s.total_amount),0) AS total_revenue,
            COUNT(DISTINCT a.date) AS days_present,
            COALESCE(sal.amount,0) AS salary
        FROM users u
        LEFT JOIN branches b
            ON u.branch_id = b.id
        LEFT JOIN sales s
            ON s.user_id = u.id
            AND strftime('%Y-%m', s.created_at) = ?
            {bf}
        LEFT JOIN attendance a
            ON a.user_id = u.id
            AND strftime('%Y-%m', a.date) = ?
        LEFT JOIN salaries sal
            ON sal.user_id = u.id
            AND sal.month = ?
        WHERE u.role != 'admin'
          AND u.active = 1
        GROUP BY u.id
        ORDER BY total_revenue DESC
    ''', (month, month, month)).fetchall()

    # Convert SQLite Rows to dictionaries
    perf = [dict(row) for row in perf]

    conn.close()

    return render_template(
        'staff_performance.html',
        performance=perf,
        month=month
    )

        # Today's sales
    ts = conn.execute('''
            SELECT
                COALESCE(SUM(total_amount),0) AS revenue,
                COUNT(*) AS transactions
            FROM sales
            WHERE branch_id=?
              AND DATE(created_at)=DATE('now')
        ''', (bid,)).fetchone()

        # Monthly sales
    ms = conn.execute('''
            SELECT
                COALESCE(SUM(total_amount),0) AS revenue,
                COUNT(*) AS transactions
            FROM sales
            WHERE branch_id=?
              AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')
        ''', (bid,)).fetchone()

        # Monthly expenses
    me = conn.execute('''
            SELECT COALESCE(SUM(amount),0)
            FROM expenses
            WHERE branch_id=?
              AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')
        ''', (bid,)).fetchone()[0]

        # Cost of goods sold
    mc = conn.execute('''
            SELECT COALESCE(SUM(si.quantity * p.buying_price),0)
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.id
            JOIN products p ON si.product_id = p.id
            WHERE s.branch_id=?
              AND strftime('%Y-%m',s.created_at)=strftime('%Y-%m','now')
        ''', (bid,)).fetchone()[0]

        # Active staff
    sc = conn.execute('''
            SELECT COUNT(*)
            FROM users
            WHERE branch_id=?
              AND active=1
              AND role!='admin'
        ''', (bid,)).fetchone()[0]

        # Low stock items
    ls = conn.execute('''
            SELECT COUNT(*)
            FROM products
            WHERE branch_id=?
              AND stock_qty<=min_stock
              AND active=1
        ''', (bid,)).fetchone()[0]

    gross = ms['revenue'] - mc
    net = gross - me

    branch_stats.append({
            'branch': dict(b),
            'today_revenue': ts['revenue'],
            'today_txns': ts['transactions'],
            'month_revenue': ms['revenue'],
            'month_txns': ms['transactions'],
            'month_expenses': me,
            'gross_profit': gross,
            'net_profit': net,
            'staff_count': sc,
            'low_stock': ls
        })

    # Totals
    totals = {
        'today_revenue': sum(x['today_revenue'] for x in branch_stats),
        'month_revenue': sum(x['month_revenue'] for x in branch_stats),
        'month_expenses': sum(x['month_expenses'] for x in branch_stats),
        'net_profit': sum(x['net_profit'] for x in branch_stats),
        'staff': sum(x['staff_count'] for x in branch_stats),
        'low_stock': sum(x['low_stock'] for x in branch_stats),
    }

    # Best performing branch
    best = max(
        branch_stats,
        key=lambda x: x['month_revenue'],
        default=None
    )

    # Weekly sales data for chart
    weekly_rows = conn.execute('''
        SELECT
            DATE(s.created_at) AS day,
            b.name AS branch_name,
            COALESCE(SUM(s.total_amount),0) AS total
        FROM sales s
        JOIN branches b
            ON s.branch_id = b.id
        WHERE s.created_at >= DATE('now','-6 days')
        GROUP BY DATE(s.created_at), s.branch_id
        ORDER BY day
    ''').fetchall()

    weekly = [dict(row) for row in weekly_rows]

    # Top debtors
    debtor_rows = conn.execute('''
        SELECT
            c.*,
            b.name AS branch_name
        FROM customers c
        LEFT JOIN branches b
            ON c.branch_id = b.id
        WHERE c.debit_balance > 0
        ORDER BY c.debit_balance DESC
        LIMIT 10
    ''').fetchall()

    debtors = [dict(row) for row in debtor_rows]

    conn.close()

    return render_template(
        'branch_overview.html',
        branch_stats=branch_stats,
        totals=totals,
        best=best,
        weekly=weekly,
        debtors=debtors
    )
# ── Mobile Money ───────────────────────────────────────
@app.route('/mobile_money')
@login_required
def mobile_money():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    date_from = request.args.get('from', datetime.now().strftime('%Y-%m-%d'))
    date_to   = request.args.get('to',   datetime.now().strftime('%Y-%m-%d'))
    provider  = request.args.get('provider','')
    bf2       = get_bf2(role, branch_id)
    pf        = f"AND provider='{provider}'" if provider else ''

    try:
        txns = conn.execute(f'''
            SELECT m.*,b.name as branch_name,u.full_name as recorded_by_name
            FROM mobile_money_transactions m
            LEFT JOIN branches b ON m.branch_id=b.id
            LEFT JOIN users u ON m.recorded_by=u.id
            WHERE DATE(m.created_at) BETWEEN ? AND ? {bf2} {pf}
            ORDER BY m.created_at DESC
        ''', (date_from,date_to)).fetchall()

        summary = conn.execute(f'''
            SELECT provider,
                   COALESCE(SUM(CASE WHEN type='credit' THEN amount ELSE 0 END),0) as total_credited,
                   COALESCE(SUM(CASE WHEN type='debit'  THEN amount ELSE 0 END),0) as total_debited,
                   COUNT(*) as count
            FROM mobile_money_transactions m
            WHERE DATE(created_at) BETWEEN ? AND ? {bf2}
            GROUP BY provider
        ''', (date_from,date_to)).fetchall()

        overall = conn.execute(f'''
            SELECT COALESCE(SUM(CASE WHEN type='credit' THEN amount ELSE 0 END),0) as total_credited,
                   COALESCE(SUM(CASE WHEN type='debit'  THEN amount ELSE 0 END),0) as total_debited,
                   COUNT(*) as count
            FROM mobile_money_transactions m
            WHERE DATE(created_at) BETWEEN ? AND ? {bf2}
        ''', (date_from,date_to)).fetchone()
    except:
        txns    = []
        summary = []
        overall = {'total_credited':0,'total_debited':0,'count':0}

    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('mobile_money.html',
        txns=txns, summary=summary, overall=overall,
        date_from=date_from, date_to=date_to,
        provider=provider, branches=branches)

@app.route('/mobile_money/add', methods=['POST'])
@login_required
def add_mobile_money():
    conn = get_db()
    try:
        conn.execute('''INSERT INTO mobile_money_transactions
            (branch_id,provider,type,phone_number,amount,reference,description,recorded_by)
            VALUES (?,?,?,?,?,?,?,?)''',
            (request.form.get('branch_id', session['branch_id']),
             request.form['provider'], request.form['type'],
             request.form.get('phone_number',''),
             float(request.form['amount']),
             request.form.get('reference',''),
             request.form.get('description',''),
             session['user_id']))
        conn.commit(); flash('Transaction recorded!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}. Make sure database is updated.', 'error')
    conn.close()
    return redirect(url_for('mobile_money'))

@app.route('/mobile_money/delete/<int:mid>', methods=['POST'])
@login_required
def delete_mobile_money(mid):
    conn = get_db()
    conn.execute('DELETE FROM mobile_money_transactions WHERE id=?', (mid,))
    conn.commit(); conn.close()
    flash('Transaction deleted!', 'success')
    return redirect(url_for('mobile_money'))

# ── API ────────────────────────────────────────────────
@app.route('/api/stats')
@login_required
def api_stats():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    bf2       = get_bf2(role, branch_id)
    sw        = conn.execute(f'''
        SELECT DATE(created_at) as day,COALESCE(SUM(total_amount),0) as total
        FROM sales WHERE created_at>=DATE('now','-6 days') {bf2}
        GROUP BY DATE(created_at) ORDER BY day
    ''').fetchall()
    conn.close()
    return jsonify({'sales_week':[{'day':r['day'],'total':r['total']} for r in sw]})

@app.route('/api/branch_chart')
@login_required
def api_branch_chart():
    conn = get_db()
    data = []
    for b in conn.execute('SELECT * FROM branches').fetchall():
        rev = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) FROM sales
            WHERE branch_id=? AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')
        ''', (b['id'],)).fetchone()[0]
        data.append({'name':b['name'],'revenue':rev})
    conn.close()
    return jsonify(data)

@app.route('/api/product/<barcode>')
@login_required
def api_product(barcode):
    conn = get_db()
    p    = conn.execute('SELECT * FROM products WHERE barcode=? AND active=1', (barcode,)).fetchone()
    conn.close()
    if p:
        return jsonify({'found':True,'id':p['id'],'name':p['name'],
                        'price':p['selling_price'],'stock':p['stock_qty'],'unit':p['unit']})
    return jsonify({'found':False})

# ── Main ───────────────────────────────────────────────
if __name__ == '__main__':
    print("="*50)
    print("  NODDY STORE POS SYSTEM")
    print("="*50)
    init_db()
    print("\nOpen: http://localhost:5001\n")
    app.run(host='0.0.0.0', port=5001, debug=True)