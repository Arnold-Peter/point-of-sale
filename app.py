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

# ── Auth helpers ───────────────────────────────────────
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

# ── Auth routes ────────────────────────────────────────
@app.route('/login', methods=['GET','POST'])
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
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    today     = datetime.now().strftime('%Y-%m-%d')

    if role == 'admin':
        total_products = conn.execute(
            'SELECT COUNT(*) FROM products WHERE active=1'
        ).fetchone()[0]
        sales_today = conn.execute('''
            SELECT COALESCE(SUM(s.total_amount),0) as revenue,
                   COUNT(s.id) as txns,
                   COALESCE(SUM(si.quantity*p.buying_price),0) as cost
            FROM sales s
            LEFT JOIN sale_items si ON si.sale_id=s.id
            LEFT JOIN products p ON si.product_id=p.id
            WHERE DATE(s.created_at)=?
        ''', (today,)).fetchone()
        total_customers = conn.execute(
            'SELECT COUNT(*) FROM customers'
        ).fetchone()[0]
        total_staff = conn.execute(
            'SELECT COUNT(*) FROM users WHERE active=1 AND role!="admin"'
        ).fetchone()[0]
        low_stock = conn.execute(
            'SELECT COUNT(*) FROM products WHERE stock_qty<=min_stock AND active=1'
        ).fetchone()[0]
        branches = conn.execute('SELECT * FROM branches').fetchall()
        recent_sales = conn.execute('''
            SELECT s.*,u.full_name as cashier,b.name as branch_name
            FROM sales s
            LEFT JOIN users u ON s.user_id=u.id
            LEFT JOIN branches b ON s.branch_id=b.id
            ORDER BY s.created_at DESC LIMIT 8
        ''').fetchall()
        expenses_today = conn.execute('''
            SELECT COALESCE(SUM(amount),0) FROM expenses
            WHERE DATE(created_at)=?
        ''', (today,)).fetchone()[0]
        debit_customers = conn.execute(
            'SELECT COUNT(*) FROM customers WHERE debit_balance>0'
        ).fetchone()[0]
    else:
        total_products = conn.execute(
            'SELECT COUNT(*) FROM products WHERE active=1 AND branch_id=?',
            (branch_id,)
        ).fetchone()[0]
        sales_today = conn.execute('''
            SELECT COALESCE(SUM(s.total_amount),0) as revenue,
                   COUNT(s.id) as txns,
                   COALESCE(SUM(si.quantity*p.buying_price),0) as cost
            FROM sales s
            LEFT JOIN sale_items si ON si.sale_id=s.id
            LEFT JOIN products p ON si.product_id=p.id
            WHERE DATE(s.created_at)=? AND s.branch_id=?
        ''', (today, branch_id)).fetchone()
        total_customers = conn.execute(
            'SELECT COUNT(*) FROM customers WHERE branch_id=?',
            (branch_id,)
        ).fetchone()[0]
        total_staff = conn.execute(
            'SELECT COUNT(*) FROM users WHERE active=1 AND branch_id=? AND role!="admin"',
            (branch_id,)
        ).fetchone()[0]
        low_stock = conn.execute(
            'SELECT COUNT(*) FROM products WHERE stock_qty<=min_stock AND active=1 AND branch_id=?',
            (branch_id,)
        ).fetchone()[0]
        branches = conn.execute(
            'SELECT * FROM branches WHERE id=?', (branch_id,)
        ).fetchall()
        recent_sales = conn.execute('''
            SELECT s.*,u.full_name as cashier,b.name as branch_name
            FROM sales s
            LEFT JOIN users u ON s.user_id=u.id
            LEFT JOIN branches b ON s.branch_id=b.id
            WHERE s.branch_id=?
            ORDER BY s.created_at DESC LIMIT 8
        ''', (branch_id,)).fetchall()
        expenses_today = conn.execute('''
            SELECT COALESCE(SUM(amount),0) FROM expenses
            WHERE DATE(created_at)=? AND branch_id=?
        ''', (today, branch_id)).fetchone()[0]
        debit_customers = conn.execute(
            'SELECT COUNT(*) FROM customers WHERE debit_balance>0 AND branch_id=?',
            (branch_id,)
        ).fetchone()[0]

    revenue_today  = sales_today['revenue']
    cost_today     = sales_today['cost']
    gross_today    = revenue_today - cost_today
    net_today      = gross_today - expenses_today
    txns_today     = sales_today['txns']

    conn.close()
    return render_template('dashboard.html',
        total_products=total_products,
        revenue_today=revenue_today,
        cost_today=cost_today,
        gross_today=gross_today,
        net_today=net_today,
        txns_today=txns_today,
        expenses_today=expenses_today,
        total_customers=total_customers,
        total_staff=total_staff,
        low_stock=low_stock,
        debit_customers=debit_customers,
        branches=branches,
        recent_sales=recent_sales,
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
                 (request.form['name'],
                  request.form.get('location',''),
                  request.form.get('phone','')))
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
        SELECT u.*,b.name as branch_name
        FROM users u LEFT JOIN branches b ON u.branch_id=b.id
        ORDER BY u.full_name
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
                     (request.form['full_name'],
                      request.form['username'],
                      hash_password(request.form['password']),
                      request.form['role'],
                      request.form['branch_id']))
        conn.commit()
        flash('User added!', 'success')
    except:
        flash('Username already exists!', 'error')
    conn.close()
    return redirect(url_for('users'))

@app.route('/users/toggle/<int:uid>', methods=['POST'])
@login_required
def toggle_user(uid):
    conn = get_db()
    conn.execute('UPDATE users SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?', (uid,))
    conn.commit(); conn.close()
    flash('User status updated!', 'success')
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
            FROM departments d
            LEFT JOIN branches b ON d.branch_id=b.id
            LEFT JOIN categories c ON c.department_id=d.id
            GROUP BY d.id ORDER BY d.name
        ''').fetchall()
    else:
        depts = conn.execute('''
            SELECT d.*,b.name as branch_name,COUNT(c.id) as cat_count
            FROM departments d
            LEFT JOIN branches b ON d.branch_id=b.id
            LEFT JOIN categories c ON c.department_id=d.id
            WHERE d.branch_id=?
            GROUP BY d.id ORDER BY d.name
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
            FROM categories c
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN products p ON p.category_id=c.id AND p.active=1
            GROUP BY c.id ORDER BY c.name
        ''').fetchall()
        depts = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    else:
        cats  = conn.execute('''
            SELECT c.*,d.name as dept_name,COUNT(p.id) as product_count
            FROM categories c
            LEFT JOIN departments d ON c.department_id=d.id
            LEFT JOIN products p ON p.category_id=c.id AND p.active=1
            WHERE d.branch_id=?
            GROUP BY c.id ORDER BY c.name
        ''', (branch_id,)).fetchall()
        depts = conn.execute(
            'SELECT * FROM departments WHERE branch_id=?', (branch_id,)
        ).fetchall()
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

    query  = '''SELECT p.*,c.name as cat_name,d.name as dept_name,b.name as branch_name
                FROM products p
                LEFT JOIN categories c ON p.category_id=c.id
                LEFT JOIN departments d ON c.department_id=d.id
                LEFT JOIN branches b ON p.branch_id=b.id
                WHERE p.active=1'''
    params = []
    if role != 'admin':
        query += ' AND p.branch_id=?'; params.append(branch_id)
    if search:
        query += ' AND (p.name LIKE ? OR p.barcode LIKE ?)'; params += [f'%{search}%',f'%{search}%']
    if cat_id:
        query += ' AND p.category_id=?'; params.append(cat_id)
    if dept_id:
        query += ' AND d.id=?'; params.append(dept_id)
    query += ' ORDER BY p.name'

    prods    = conn.execute(query, params).fetchall()
    cats     = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    depts    = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('products.html',
        prods=prods, cats=cats, depts=depts,
        branches=branches, search=search,
        cat_id=cat_id, dept_id=dept_id)

@app.route('/products/add', methods=['POST'])
@login_required
def add_product():
    conn = get_db()
    try:
        conn.execute('''INSERT INTO products
            (name,barcode,category_id,branch_id,buying_price,
             selling_price,stock_qty,min_stock,unit,description)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (request.form['name'],
             request.form.get('barcode') or None,
             request.form['category_id'],
             request.form['branch_id'],
             float(request.form.get('buying_price',0)),
             float(request.form.get('selling_price',0)),
             int(request.form.get('stock_qty',0)),
             int(request.form.get('min_stock',5)),
             request.form.get('unit','pcs'),
             request.form.get('description','')))
        conn.commit()
        flash('Product added!', 'success')
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
            buying_price=?,selling_price=?,
            stock_qty=?,min_stock=?,unit=?,description=?
            WHERE id=?''',
            (request.form['name'],
             request.form.get('barcode') or None,
             request.form['category_id'],
             request.form['branch_id'],
             float(request.form.get('buying_price',0)),
             float(request.form.get('selling_price',0)),
             int(request.form.get('stock_qty',0)),
             int(request.form.get('min_stock',5)),
             request.form.get('unit','pcs'),
             request.form.get('description',''),
             pid))
        conn.commit(); conn.close()
        flash('Product updated!', 'success')
        return redirect(url_for('products'))
    prod     = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    cats     = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    depts    = conn.execute('SELECT * FROM departments ORDER BY name').fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('edit_product.html',
                           prod=prod, cats=cats, depts=depts, branches=branches)

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

    bf = '' if role == 'admin' else f'AND p.branch_id={branch_id}'

    low_stock = conn.execute(f'''
        SELECT p.*,c.name as cat_name,d.name as dept_name,b.name as branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.stock_qty<=p.min_stock AND p.active=1 {bf}
        ORDER BY p.stock_qty ASC
    ''').fetchall()

    all_stock = conn.execute(f'''
        SELECT p.*,c.name as cat_name,d.name as dept_name,b.name as branch_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        LEFT JOIN branches b ON p.branch_id=b.id
        WHERE p.active=1 {bf}
        ORDER BY p.name
    ''').fetchall()

    sf = '' if role == 'admin' else f'AND s.branch_id={branch_id}'
    top_selling = conn.execute(f'''
        SELECT p.name,c.name as cat_name,
               SUM(si.quantity) as total_sold,
               SUM(si.total_price) as total_revenue
        FROM sale_items si
        JOIN products p ON si.product_id=p.id
        JOIN sales s ON si.sale_id=s.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE 1=1 {sf}
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
                           WHERE name=(SELECT name FROM products WHERE id=?)
                             AND branch_id=?''', (pid,to_b)).fetchone()
    if dest:
        conn.execute('UPDATE products SET stock_qty=stock_qty+? WHERE id=?', (qty,dest['id']))
    conn.execute('''INSERT INTO stock_transfers
                    (product_id,from_branch,to_branch,quantity,transferred_by)
                    VALUES (?,?,?,?,?)''',
                 (pid,from_b,to_b,qty,session['user_id']))
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
    if role == 'admin':
        custs = conn.execute('''
            SELECT c.*,b.name as branch_name FROM customers c
            LEFT JOIN branches b ON c.branch_id=b.id
            ORDER BY c.full_name
        ''').fetchall()
    else:
        custs = conn.execute('''
            SELECT c.*,b.name as branch_name FROM customers c
            LEFT JOIN branches b ON c.branch_id=b.id
            WHERE c.branch_id=? ORDER BY c.full_name
        ''', (branch_id,)).fetchall()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    conn.close()
    return render_template('customers.html', custs=custs, branches=branches)

@app.route('/customers/add', methods=['POST'])
@login_required
def add_customer():
    conn = get_db()
    conn.execute('''INSERT INTO customers
                    (full_name,phone,email,address,debit_limit,branch_id)
                    VALUES (?,?,?,?,?,?)''',
                 (request.form['full_name'],
                  request.form.get('phone',''),
                  request.form.get('email',''),
                  request.form.get('address',''),
                  float(request.form.get('debit_limit',0)),
                  request.form['branch_id']))
    conn.commit(); conn.close()
    flash('Customer added!', 'success')
    return redirect(url_for('customers'))

@app.route('/customers/pay/<int:cid>', methods=['POST'])
@login_required
def customer_pay(cid):
    conn   = get_db()
    amount = float(request.form.get('amount',0))
    conn.execute('UPDATE customers SET debit_balance=MAX(0,debit_balance-?) WHERE id=?',
                 (amount,cid))
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
    bf        = '' if role == 'admin' else f'AND p.branch_id={branch_id}'
    products  = conn.execute(f'''
        SELECT p.*,c.name as cat_name,d.name as dept_name
        FROM products p
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN departments d ON c.department_id=d.id
        WHERE p.active=1 AND p.stock_qty>0 {bf}
        ORDER BY p.name
    ''').fetchall()
    if role == 'admin':
        customers  = conn.execute('SELECT * FROM customers ORDER BY full_name').fetchall()
    else:
        customers  = conn.execute(
            'SELECT * FROM customers WHERE branch_id=? ORDER BY full_name',
            (branch_id,)
        ).fetchall()
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    conn.close()
    return render_template('pos.html',
        products=products, customers=customers, categories=categories)

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
        prod = conn.execute('SELECT stock_qty,name FROM products WHERE id=?',
                            (item['id'],)).fetchone()
        if not prod or prod['stock_qty'] < item['qty']:
            conn.close()
            return jsonify({'success':False,
                            'message':f'Insufficient stock for {prod["name"] if prod else "product"}!'})

    if payment == 'credit':
        if not customer_id:
            conn.close()
            return jsonify({'success':False,'message':'Select a customer for credit sale!'})
        cust = conn.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
        new_balance = cust['debit_balance'] + total
        if cust['debit_limit'] > 0 and new_balance > cust['debit_limit']:
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
        conn.execute('''INSERT INTO sale_items
                        (sale_id,product_id,quantity,unit_price,total_price)
                        VALUES (?,?,?,?,?)''',
                     (sale_id,item['id'],item['qty'],item['price'],item['price']*item['qty']))
        conn.execute('UPDATE products SET stock_qty=stock_qty-? WHERE id=?',
                     (item['qty'],item['id']))
        pname = conn.execute('SELECT name FROM products WHERE id=?',
                             (item['id'],)).fetchone()['name']
        items_data.append({'name':pname,'qty':item['qty'],
                           'price':item['price'],'total':item['price']*item['qty']})

    if payment == 'credit' and customer_id:
        conn.execute('UPDATE customers SET debit_balance=debit_balance+? WHERE id=?',
                     (total,customer_id))
    conn.commit(); conn.close()
    return jsonify({
        'success':True,'receipt_no':receipt_no,
        'total':total,'paid':paid_amount,'change':change,
        'payment':payment,'items':items_data,
        'cashier':session['full_name'],
        'branch':session['branch_name'],
        'datetime':datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
    bf        = '' if role == 'admin' else f'AND s.branch_id={branch_id}'

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
        SELECT COALESCE(SUM(total_amount),0) as total, COUNT(*) as count
        FROM sales s
        WHERE DATE(created_at) BETWEEN ? AND ? {bf}
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
    bf        = '' if role == 'admin' else f'AND e.branch_id={branch_id}'
    bf2       = '' if role == 'admin' else f'AND branch_id={branch_id}'

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
        SELECT category, COALESCE(SUM(amount),0) as total
        FROM expenses
        WHERE strftime('%Y-%m',created_at)=? {bf2}
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
    conn.execute('''INSERT INTO expenses
                    (branch_id,category,description,amount,recorded_by)
                    VALUES (?,?,?,?,?)''',
                 (request.form.get('branch_id', session['branch_id']),
                  request.form['category'],
                  request.form['description'],
                  float(request.form['amount']),
                  session['user_id']))
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

    bf  = '' if role == 'admin' else f'AND s.branch_id={branch_id}'
    bf2 = '' if role == 'admin' else f'AND branch_id={branch_id}'

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
        FROM sales s
        WHERE DATE(created_at) BETWEEN ? AND ? {bf}
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
        FROM expenses
        WHERE DATE(created_at) BETWEEN ? AND ? {bf2}
        GROUP BY category ORDER BY total DESC
    ''', (date_from,date_to)).fetchall()

    advice = generate_expense_advice(
        revenue, gross_profit, net_profit,
        total_expenses, margin, exp_by_cat
    )
    conn.close()
    return render_template('reports.html',
        period=period, date_from=date_from, date_to=date_to,
        revenue=revenue, cost=cost,
        gross_profit=gross_profit, net_profit=net_profit,
        total_expenses=total_expenses, margin=margin,
        daily_sales=daily_sales, top_products=top_products,
        top_categories=top_categories, exp_by_cat=exp_by_cat,
        advice=advice, transactions=sales_data['transactions'])

def generate_expense_advice(revenue, gross_profit,
                             net_profit, total_expenses, margin, exp_by_cat):
    advice = []
    if revenue == 0:
        return [{'type':'info','title':'No sales data yet',
                 'message':'Record some sales to get financial advice.'}]
    exp_ratio = (total_expenses/revenue*100) if revenue > 0 else 0
    if net_profit < 0:
        advice.append({'type':'danger','title':'Business is running at a loss!',
            'message':f'You are losing TZS {abs(net_profit):,.0f}. Reduce expenses or increase prices immediately.'})
    elif net_profit < gross_profit*0.1:
        advice.append({'type':'warning','title':'Net profit is very low',
            'message':f'Net margin is only {(net_profit/revenue*100):.1f}%. Consider reducing operational costs.'})
    else:
        advice.append({'type':'success','title':'Business is profitable!',
            'message':f'Net profit TZS {net_profit:,.0f} ({(net_profit/revenue*100):.1f}% margin). Keep it up!'})
    if exp_ratio > 40:
        advice.append({'type':'danger','title':'Expenses are too high',
            'message':f'Expenses are {exp_ratio:.1f}% of revenue. Target is below 30%. Minimize immediately.'})
    elif exp_ratio > 25:
        advice.append({'type':'warning','title':'Expenses are moderate',
            'message':f'Expenses are {exp_ratio:.1f}% of revenue. Look for areas to cut costs.'})
    else:
        advice.append({'type':'success','title':'Expenses well controlled',
            'message':f'Expenses are only {exp_ratio:.1f}% of revenue. Excellent management!'})
    if margin < 10:
        advice.append({'type':'danger','title':'Gross margin critically low',
            'message':f'Only {margin:.1f}% gross margin. Review buying prices or increase selling prices.'})
    elif margin < 20:
        advice.append({'type':'warning','title':'Gross margin needs improvement',
            'message':f'{margin:.1f}% gross margin. Negotiate better prices with suppliers.'})
    else:
        advice.append({'type':'success','title':'Good gross margin',
            'message':f'{margin:.1f}% gross margin is healthy. Keep monitoring pricing.'})
    if exp_by_cat:
        top_exp = exp_by_cat[0]
        if top_exp['total'] > revenue*0.15:
            advice.append({'type':'warning',
                'title':f'High spending on {top_exp["category"]}',
                'message':f'TZS {top_exp["total"]:,.0f} on {top_exp["category"]} '
                          f'({(top_exp["total"]/revenue*100):.1f}% of revenue). Consider reducing.'})
    return advice

@app.route('/reports/pdf')
@login_required
def report_pdf():
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate,Table,
            TableStyle,Paragraph,Spacer)
        from reportlab.lib.styles import getSampleStyleSheet
        import io
        period    = request.args.get('period','daily')
        date_from = request.args.get('from')
        date_to   = request.args.get('to')
        conn      = get_db()
        branch_id = session.get('branch_id')
        role      = session.get('role')
        bf        = '' if role=='admin' else f'AND s.branch_id={branch_id}'
        bf2       = '' if role=='admin' else f'AND branch_id={branch_id}'
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
            SELECT p.name,SUM(si.quantity) as qty,SUM(si.total_price) as rev
            FROM sale_items si
            JOIN sales s ON si.sale_id=s.id
            JOIN products p ON si.product_id=p.id
            WHERE DATE(s.created_at) BETWEEN ? AND ? {bf}
            GROUP BY p.id ORDER BY qty DESC LIMIT 15
        ''', (date_from,date_to)).fetchall()
        conn.close()
        buf  = io.BytesIO()
        doc  = SimpleDocTemplate(buf, pagesize=A4, topMargin=40, bottomMargin=40)
        styl = getSampleStyleSheet()
        els  = []
        els.append(Paragraph('NODDY STORE — FINANCIAL REPORT', styl['Title']))
        els.append(Paragraph(f'Period: {date_from} to {date_to} | Generated by: {session["full_name"]}', styl['Normal']))
        els.append(Spacer(1,16))
        els.append(Paragraph('Profit & Loss Summary', styl['Heading2']))
        pl = [['Item','Amount (TZS)'],
              ['Total Revenue',f'{revenue:,.0f}'],
              ['Cost of Goods',f'{cost:,.0f}'],
              ['Gross Profit', f'{gross_profit:,.0f}'],
              ['Total Expenses',f'{te:,.0f}'],
              ['NET PROFIT / LOSS',f'{net_profit:,.0f}']]
        t = Table(pl, colWidths=[300,180])
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1a1a2e')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('BACKGROUND',(0,5),(-1,5),
             colors.HexColor('#1a4a1a') if net_profit>=0 else colors.HexColor('#4a1a1a')),
            ('TEXTCOLOR',(0,5),(-1,5),colors.white),
            ('FONTNAME',(0,5),(-1,5),'Helvetica-Bold'),
            ('GRID',(0,0),(-1,-1),0.5,colors.grey),
            ('FONTSIZE',(0,0),(-1,-1),11),
            ('ROWBACKGROUNDS',(0,1),(-1,4),[colors.white,colors.HexColor('#f9f9f9')]),
        ]))
        els.append(t); els.append(Spacer(1,20))
        if tp:
            els.append(Paragraph('Top Products', styl['Heading2']))
            data = [['Product','Qty Sold','Revenue (TZS)']]
            for p in tp:
                data.append([p['name'],str(p['qty']),f'{p["rev"]:,.0f}'])
            t2 = Table(data, colWidths=[250,100,130])
            t2.setStyle(TableStyle([
                ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f39c12')),
                ('TEXTCOLOR',(0,0),(-1,0),colors.black),
                ('GRID',(0,0),(-1,-1),0.5,colors.grey),
                ('FONTSIZE',(0,0),(-1,-1),10),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#fef9f0')]),
            ]))
            els.append(t2)
        doc.build(els)
        buf.seek(0)
        return send_file(buf, mimetype='application/pdf', as_attachment=True,
                        download_name=f'noddy_report_{date_from}_{date_to}.pdf')
    except ImportError:
        return 'Run: pip install reportlab', 500

# ── Staff ──────────────────────────────────────────────
@app.route('/staff')
@login_required
def staff():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    bf        = '' if role=='admin' else f'AND u.branch_id={branch_id}'
    bf2       = '' if role=='admin' else f'AND branch_id={branch_id}'

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
        WHERE u.role!='admin' {bf}
        GROUP BY u.id ORDER BY u.full_name
    ''').fetchall()

    branches = conn.execute('SELECT * FROM branches').fetchall()
    recommendation = get_staff_recommendation(conn, branch_id if role!='admin' else None)

    salary_total = conn.execute(f'''
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
        salary_total=salary_total,
        attendance_today=attendance_today)

def get_staff_recommendation(conn, branch_id=None):
    bf  = f'AND branch_id={branch_id}' if branch_id else ''
    bf2 = f'AND users.branch_id={branch_id}' if branch_id else ''
    avg_txns = conn.execute(f'''
        SELECT AVG(daily_count) FROM (
            SELECT COUNT(*) as daily_count FROM sales
            WHERE created_at>=DATE('now','-30 days') {bf}
            GROUP BY DATE(created_at)
        )
    ''').fetchone()[0] or 0
    staff_count = conn.execute(f'''
        SELECT COUNT(*) FROM users
        WHERE active=1 AND role!='admin' {bf2}
    ''').fetchone()[0]
    recommended = max(1, round(avg_txns/20))
    if staff_count < recommended:
        status  = 'increase'
        message = (f'Based on {avg_txns:.0f} avg daily transactions, '
                   f'you need {recommended} staff. Hire {recommended-staff_count} more.')
    elif staff_count > recommended+2:
        status  = 'reduce'
        message = (f'You have {staff_count} staff for {avg_txns:.0f} daily transactions. '
                   f'You may reduce by {staff_count-recommended} to save costs.')
    else:
        status  = 'optimal'
        message = (f'Your {staff_count} staff is optimal for '
                   f'{avg_txns:.0f} daily transactions. No changes needed.')
    return {'status':status,'message':message,
            'current':staff_count,'recommended':recommended,'avg_txns':round(avg_txns,1)}

@app.route('/staff/attendance', methods=['POST'])
@login_required
def mark_attendance():
    conn = get_db()
    conn.execute('''INSERT OR REPLACE INTO attendance
                    (user_id,branch_id,date,check_in,status)
                    VALUES (?,?,DATE('now'),TIME('now'),?)''',
                 (request.form['user_id'],
                  session['branch_id'],
                  request.form.get('status','present')))
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
        conn.execute('''INSERT OR REPLACE INTO salaries
                        (user_id,branch_id,amount,month)
                        VALUES (?,?,?,strftime('%Y-%m','now'))''',
                     (request.form['user_id'],
                      session.get('branch_id',1),
                      float(request.form['amount'])))
        conn.commit()
        flash('Salary set!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    conn.close()
    return redirect(url_for('staff'))

@app.route('/staff/salary/pay/<int:uid>', methods=['POST'])
@login_required
def pay_salary(uid):
    conn   = get_db()
    staff  = conn.execute('SELECT full_name FROM users WHERE id=?', (uid,)).fetchone()
    salary = conn.execute('''SELECT amount FROM salaries
                             WHERE user_id=? AND month=strftime('%Y-%m','now')''',
                          (uid,)).fetchone()
    conn.execute('''UPDATE salaries SET paid=1,paid_at=CURRENT_TIMESTAMP
                    WHERE user_id=? AND month=strftime('%Y-%m','now')''', (uid,))
    if salary:
        conn.execute('''INSERT INTO expenses (branch_id,category,description,amount,recorded_by)
                        VALUES (?,?,?,?,?)''',
                     (session.get('branch_id',1),'Salaries',
                      f'Salary — {staff["full_name"]}',
                      salary['amount'],session['user_id']))
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
    bf        = '' if role=='admin' else f'AND a.branch_id={branch_id}'
    records   = conn.execute(f'''
        SELECT a.*,u.full_name,b.name as branch_name
        FROM attendance a
        JOIN users u ON a.user_id=u.id
        LEFT JOIN branches b ON a.branch_id=b.id
        WHERE strftime('%Y-%m',a.date)=? {bf}
        ORDER BY a.date DESC,u.full_name
    ''', (month,)).fetchall()
    conn.close()
    return render_template('attendance.html', records=records, month=month)

@app.route('/staff/performance')
@login_required
def staff_performance():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    month     = request.args.get('month', datetime.now().strftime('%Y-%m'))
    bf        = '' if role=='admin' else f'AND s.branch_id={branch_id}'
    perf      = conn.execute(f'''
        SELECT u.full_name,u.role,b.name as branch_name,
               COUNT(DISTINCT s.id) as total_sales,
               COALESCE(SUM(s.total_amount),0) as total_revenue,
               COUNT(DISTINCT a.date) as days_present,
               COALESCE(sal.amount,0) as salary
        FROM users u
        LEFT JOIN branches b ON u.branch_id=b.id
        LEFT JOIN sales s ON s.user_id=u.id
            AND strftime('%Y-%m',s.created_at)=? {bf}
        LEFT JOIN attendance a ON a.user_id=u.id
            AND strftime('%Y-%m',a.date)=?
        LEFT JOIN salaries sal ON sal.user_id=u.id AND sal.month=?
        WHERE u.role!='admin' AND u.active=1
        GROUP BY u.id ORDER BY total_revenue DESC
    ''', (month,month,month)).fetchall()
    conn.close()
    return render_template('staff_performance.html', performance=perf, month=month)

# ── Branch overview ────────────────────────────────────
@app.route('/branch/overview')
@login_required
def branch_overview():
    conn     = get_db()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    branch_stats = []
    for b in branches:
        ts = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) as revenue,COUNT(*) as transactions
            FROM sales WHERE branch_id=? AND DATE(created_at)=DATE('now')
        ''', (b['id'],)).fetchone()
        ms = conn.execute('''
            SELECT COALESCE(SUM(total_amount),0) as revenue,COUNT(*) as transactions
            FROM sales WHERE branch_id=?
              AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')
        ''', (b['id'],)).fetchone()
        me = conn.execute('''
            SELECT COALESCE(SUM(amount),0) FROM expenses
            WHERE branch_id=? AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')
        ''', (b['id'],)).fetchone()[0]
        sc = conn.execute(
            'SELECT COUNT(*) FROM users WHERE branch_id=? AND active=1 AND role!="admin"',
            (b['id'],)
        ).fetchone()[0]
        ls = conn.execute(
            'SELECT COUNT(*) FROM products WHERE branch_id=? AND stock_qty<=min_stock AND active=1',
            (b['id'],)
        ).fetchone()[0]
        mc = conn.execute('''
            SELECT COALESCE(SUM(si.quantity*p.buying_price),0)
            FROM sale_items si JOIN sales s ON si.sale_id=s.id
            JOIN products p ON si.product_id=p.id
            WHERE s.branch_id=? AND strftime('%Y-%m',s.created_at)=strftime('%Y-%m','now')
        ''', (b['id'],)).fetchone()[0]
        gross = ms['revenue']-mc
        net   = gross-me
        branch_stats.append({
            'branch':b,'today_revenue':ts['revenue'],
            'today_txns':ts['transactions'],
            'month_revenue':ms['revenue'],
            'month_txns':ms['transactions'],
            'month_expenses':me,'gross_profit':gross,
            'net_profit':net,'staff_count':sc,'low_stock':ls
        })

    totals = {
        'today_revenue':  sum(b['today_revenue']  for b in branch_stats),
        'month_revenue':  sum(b['month_revenue']  for b in branch_stats),
        'month_expenses': sum(b['month_expenses'] for b in branch_stats),
        'net_profit':     sum(b['net_profit']     for b in branch_stats),
        'staff':          sum(b['staff_count']    for b in branch_stats),
        'low_stock':      sum(b['low_stock']      for b in branch_stats),
    }
    best = max(branch_stats, key=lambda x: x['month_revenue'], default=None)
    weekly = conn.execute('''
        SELECT DATE(created_at) as day,b.name as branch_name,
               COALESCE(SUM(total_amount),0) as total
        FROM sales s JOIN branches b ON s.branch_id=b.id
        WHERE created_at>=DATE('now','-6 days')
        GROUP BY DATE(created_at),s.branch_id ORDER BY day
    ''').fetchall()
    debtors = conn.execute('''
        SELECT c.*,b.name as branch_name FROM customers c
        LEFT JOIN branches b ON c.branch_id=b.id
        WHERE c.debit_balance>0 ORDER BY c.debit_balance DESC LIMIT 10
    ''').fetchall()
    conn.close()
    return render_template('branch_overview.html',
        branch_stats=branch_stats, totals=totals,
        best=best, weekly=weekly, debtors=debtors)

# ── API ────────────────────────────────────────────────
@app.route('/api/stats')
@login_required
def api_stats():
    conn      = get_db()
    branch_id = session.get('branch_id')
    role      = session.get('role')
    bf        = '' if role=='admin' else f'AND branch_id={branch_id}'
    sales_week = conn.execute(f'''
        SELECT DATE(created_at) as day,
               COALESCE(SUM(total_amount),0) as total
        FROM sales WHERE created_at>=DATE('now','-6 days') {bf}
        GROUP BY DATE(created_at) ORDER BY day
    ''').fetchall()
    conn.close()
    return jsonify({'sales_week':[{'day':r['day'],'total':r['total']} for r in sales_week]})

@app.route('/api/branch_chart')
@login_required
def api_branch_chart():
    conn     = get_db()
    branches = conn.execute('SELECT * FROM branches').fetchall()
    data = []
    for b in branches:
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
    prod = conn.execute(
        'SELECT * FROM products WHERE barcode=? AND active=1', (barcode,)
    ).fetchone()
    conn.close()
    if prod:
        return jsonify({'found':True,'id':prod['id'],'name':prod['name'],
                        'price':prod['selling_price'],'stock':prod['stock_qty'],'unit':prod['unit']})
    return jsonify({'found':False})

# ── Main ───────────────────────────────────────────────
if __name__ == '__main__':
    print("="*50)
    print("  NODDY STORE POS SYSTEM")
    print("="*50)
    init_db()
    print("\nOpen: http://localhost:5001\n")
    app.run(host='0.0.0.0', port=5001, debug=True)