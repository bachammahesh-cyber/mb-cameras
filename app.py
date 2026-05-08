import os
import sqlite3
from collections import defaultdict
from datetime import date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')

DATABASE_URL = os.environ.get('DATABASE_URL')
DB_PATH = os.path.join(os.path.dirname(__file__), 'database.db')

DEMO_USERNAME = 'demo'
DEMO_PASSWORD = 'demo123'


def get_db():
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        return conn, True
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn, False


def q(sql, is_pg):
    return sql.replace('?', '%s') if is_pg else sql


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def calc_days(start_str, end_str):
    return (date.fromisoformat(end_str) - date.fromisoformat(start_str)).days + 1


def fmt_date(date_str):
    if not date_str or len(date_str) < 10:
        return date_str or ''
    return f"{date_str[8:10]}/{date_str[5:7]}/{date_str[2:4]}"


# ── Health check ──────────────────────────────────────────────────────────────

@app.route('/healthz')
def healthz():
    return 'OK', 200


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn, is_pg = get_db()
        try:
            cur = conn.cursor()
            cur.execute(q('SELECT * FROM users WHERE username = ?', is_pg), (username,))
            user = cur.fetchone()
            if user:
                u = dict(user)
                if check_password_hash(u['password'], password):
                    session['user_id'] = u['id']
                    session['tenant_id'] = u['tenant_id']
                    session['role'] = u['role']
                    return redirect(url_for('dashboard'))
            error = 'Invalid username or password.'
        finally:
            conn.close()
    return render_template('login.html', error=error,
                           demo_username=DEMO_USERNAME, demo_password=DEMO_PASSWORD)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', role=session.get('role'))


# ── Rentals ───────────────────────────────────────────────────────────────────

@app.route('/new_rental')
@login_required
def new_rental():
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('SELECT * FROM items WHERE tenant_id = ? ORDER BY name', is_pg), (tenant_id,))
        items = rows(cur)
        cur.execute(q('SELECT * FROM clients WHERE tenant_id = ? ORDER BY name', is_pg), (tenant_id,))
        clients = rows(cur)
    finally:
        conn.close()
    return render_template('new_rental.html', items=items, clients=clients)


def _parse_rental_form(form, tenant_id, days, conn, is_pg):
    item_ids = form.getlist('item_ids')
    vendor_item_ids = form.getlist('vendor_item_ids')
    vendor_name = form.get('vendor_name', '').strip()
    advance_paid = float(form.get('advance_paid', 0) or 0)

    cur = conn.cursor()
    total_amount = 0.0
    own_items = []
    vendor_items = []

    for item_id in item_ids:
        rate = float(form.get(f'item_rates[{item_id}]') or 0)
        total = rate * days
        total_amount += total
        own_items.append((int(item_id), rate, days, total))

    if vendor_name:
        for item_id in vendor_item_ids:
            rate = float(form.get(f'vendor_rates[{item_id}]') or 0)
            cur.execute(q('SELECT name FROM items WHERE id = ? AND tenant_id = ?', is_pg),
                        (item_id, tenant_id))
            row = cur.fetchone()
            item_name = dict(row)['name'] if row else ''
            total = rate * days
            total_amount += total
            vendor_items.append((vendor_name, item_name, rate, days, total))

    balance = total_amount - advance_paid
    return advance_paid, total_amount, balance, own_items, vendor_items


def _insert_rental_items(cur, conn, is_pg, rental_id, tenant_id, own_items, vendor_items):
    for item_id, rate, days, total in own_items:
        cur.execute(q('''INSERT INTO rental_items
                         (rental_id, item_id, rate_per_day, days, total, status, tenant_id)
                         VALUES (?, ?, ?, ?, ?, 'Active', ?)''', is_pg),
                    (rental_id, item_id, rate, days, total, tenant_id))
    for vname, item_name, rate, days, total in vendor_items:
        cur.execute(q('''INSERT INTO outside_items
                         (rental_id, vendor_name, item_name, rate_per_day, days, total, paid, balance, tenant_id)
                         VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)''', is_pg),
                    (rental_id, vname, item_name, rate, days, total, total, tenant_id))


@app.route('/save_rental', methods=['POST'])
@login_required
def save_rental():
    tenant_id = session['tenant_id']
    customer_name = request.form['customer_name']
    phone = request.form['phone']
    start_date = request.form['start_date']
    end_date = request.form['end_date']
    days = calc_days(start_date, end_date)

    conn, is_pg = get_db()
    try:
        advance_paid, total_amount, balance, own_items, vendor_items = \
            _parse_rental_form(request.form, tenant_id, days, conn, is_pg)
        cur = conn.cursor()
        cur.execute(q('''INSERT INTO rentals
                         (customer_name, phone, start_date, end_date,
                          total_amount, advance_paid, balance, status, tenant_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?, 'Active', ?)''', is_pg),
                    (customer_name, phone, start_date, end_date,
                     total_amount, advance_paid, balance, tenant_id))
        if is_pg:
            cur.execute('SELECT lastval()')
        else:
            cur.execute('SELECT last_insert_rowid()')
        rental_id = cur.fetchone()[0]
        _insert_rental_items(cur, conn, is_pg, rental_id, tenant_id, own_items, vendor_items)
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('rental_records'))


@app.route('/rental_records')
@login_required
def rental_records():
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('SELECT * FROM rentals WHERE tenant_id = ? ORDER BY id DESC', is_pg), (tenant_id,))
        raw = rows(cur)
        rentals = []
        for r in raw:
            cur.execute(q('''SELECT i.name FROM rental_items ri
                             JOIN items i ON ri.item_id = i.id
                             WHERE ri.rental_id = ?''', is_pg), (r['id'],))
            equipment_names = [row[0] for row in cur.fetchall()]
            rentals.append({
                **r,
                'equipment_names': equipment_names,
                'rental_days': calc_days(r['start_date'], r['end_date']),
                'given_date_display': fmt_date(r['start_date']),
            })
    finally:
        conn.close()
    return render_template('rental_records.html', rentals=rentals, role=session.get('role'))


@app.route('/edit_rental/<int:rental_id>', methods=['GET', 'POST'])
@login_required
def edit_rental(rental_id):
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        if request.method == 'POST':
            start_date = request.form['start_date']
            end_date = request.form['end_date']
            days = calc_days(start_date, end_date)
            advance_paid, total_amount, balance, own_items, vendor_items = \
                _parse_rental_form(request.form, tenant_id, days, conn, is_pg)
            status = request.form.get('status', 'Active')

            cur.execute(q('DELETE FROM rental_items WHERE rental_id = ?', is_pg), (rental_id,))
            cur.execute(q('DELETE FROM outside_items WHERE rental_id = ?', is_pg), (rental_id,))
            _insert_rental_items(cur, conn, is_pg, rental_id, tenant_id, own_items, vendor_items)

            cur.execute(q('''UPDATE rentals SET customer_name=?, phone=?, start_date=?, end_date=?,
                             total_amount=?, advance_paid=?, balance=?, status=?
                             WHERE id=? AND tenant_id=?''', is_pg),
                        (request.form['customer_name'], request.form['phone'],
                         start_date, end_date, total_amount, advance_paid, balance, status,
                         rental_id, tenant_id))
            conn.commit()
            return redirect(url_for('rental_records'))

        # GET — load rental + selections
        cur.execute(q('SELECT * FROM rentals WHERE id = ? AND tenant_id = ?', is_pg),
                    (rental_id, tenant_id))
        rental = dict(cur.fetchone())

        cur.execute(q('SELECT item_id, rate_per_day FROM rental_items WHERE rental_id = ?', is_pg),
                    (rental_id,))
        ri_rows = rows(cur)
        selected_item_ids = [r['item_id'] for r in ri_rows]
        selected_rates = {r['item_id']: r['rate_per_day'] for r in ri_rows}

        cur.execute(q('SELECT * FROM outside_items WHERE rental_id = ?', is_pg), (rental_id,))
        oi_rows = rows(cur)
        outside_vendor_name = oi_rows[0]['vendor_name'] if oi_rows else ''
        selected_vendor_item_names = [oi['item_name'] for oi in oi_rows]
        selected_vendor_rates = {oi['item_name']: oi['rate_per_day'] for oi in oi_rows}

        cur.execute(q('SELECT * FROM items WHERE tenant_id = ? ORDER BY name', is_pg), (tenant_id,))
        items = rows(cur)
        cur.execute(q('SELECT * FROM clients WHERE tenant_id = ? ORDER BY name', is_pg), (tenant_id,))
        clients = rows(cur)
    finally:
        conn.close()

    return render_template('edit_rental.html',
                           rental=rental, items=items, clients=clients,
                           vendor_items=items,
                           selected_item_ids=selected_item_ids,
                           selected_rates=selected_rates,
                           outside_vendor_name=outside_vendor_name,
                           selected_vendor_item_names=selected_vendor_item_names,
                           selected_vendor_rates=selected_vendor_rates)


@app.route('/toggle_rental_status/<int:rental_id>', methods=['POST'])
@login_required
def toggle_rental_status(rental_id):
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('SELECT status FROM rentals WHERE id = ? AND tenant_id = ?', is_pg),
                    (rental_id, tenant_id))
        row = cur.fetchone()
        if row:
            new_status = 'Returned' if dict(row)['status'] == 'Active' else 'Active'
            cur.execute(q('UPDATE rentals SET status = ? WHERE id = ? AND tenant_id = ?', is_pg),
                        (new_status, rental_id, tenant_id))
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for('rental_records'))


@app.route('/delete_rental/<int:rental_id>', methods=['POST'])
@login_required
def delete_rental(rental_id):
    if session.get('role') != 'owner':
        return redirect(url_for('rental_records'))
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('DELETE FROM rental_items WHERE rental_id = ?', is_pg), (rental_id,))
        cur.execute(q('DELETE FROM outside_items WHERE rental_id = ?', is_pg), (rental_id,))
        cur.execute(q('DELETE FROM rentals WHERE id = ? AND tenant_id = ?', is_pg),
                    (rental_id, tenant_id))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('rental_records'))


# ── Inventory ─────────────────────────────────────────────────────────────────

@app.route('/inventory')
@login_required
def inventory():
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('SELECT * FROM items WHERE tenant_id = ? ORDER BY name', is_pg), (tenant_id,))
        raw_items = rows(cur)
        items = []
        for item in raw_items:
            cur.execute(q('SELECT COUNT(*) FROM rental_items WHERE item_id = ?', is_pg), (item['id'],))
            count = cur.fetchone()[0]
            item['can_delete'] = count == 0
            item['delete_reason'] = '' if count == 0 else 'Item has rental history'
            items.append(item)
        cur.execute(q('SELECT * FROM clients WHERE tenant_id = ? ORDER BY name', is_pg), (tenant_id,))
        clients = rows(cur)
    finally:
        conn.close()
    return render_template('inventory.html', items=items, clients=clients)


@app.route('/add_item', methods=['POST'])
@login_required
def add_item():
    tenant_id = session['tenant_id']
    name = request.form['name']
    category = request.form.get('category', '')
    rent = float(request.form.get('rent', 0) or 0)
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('''INSERT INTO items (name, category, rent_per_day, status, tenant_id)
                         VALUES (?, ?, ?, 'Available', ?)''', is_pg),
                    (name, category, rent, tenant_id))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('inventory'))


@app.route('/edit_item/<int:item_id>', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        if request.method == 'POST':
            cur.execute(q('UPDATE items SET name=?, category=?, rent_per_day=? WHERE id=? AND tenant_id=?', is_pg),
                        (request.form['name'], request.form.get('category', ''),
                         float(request.form.get('rent', 0) or 0), item_id, tenant_id))
            conn.commit()
            return redirect(url_for('inventory'))
        cur.execute(q('SELECT * FROM items WHERE id = ? AND tenant_id = ?', is_pg), (item_id, tenant_id))
        item = dict(cur.fetchone())
    finally:
        conn.close()
    return render_template('edit_item.html', item=item)


@app.route('/delete_item/<int:item_id>', methods=['POST'])
@login_required
def delete_item(item_id):
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('SELECT COUNT(*) FROM rental_items WHERE item_id = ?', is_pg), (item_id,))
        if cur.fetchone()[0] == 0:
            cur.execute(q('DELETE FROM items WHERE id = ? AND tenant_id = ?', is_pg), (item_id, tenant_id))
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for('inventory'))


@app.route('/add_client', methods=['POST'])
@login_required
def add_client():
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('INSERT INTO clients (name, phone, address, tenant_id) VALUES (?, ?, ?, ?)', is_pg),
                    (request.form['name'], request.form['phone'],
                     request.form.get('address', ''), tenant_id))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('inventory'))


@app.route('/edit_client/<int:client_id>', methods=['GET', 'POST'])
@login_required
def edit_client(client_id):
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        if request.method == 'POST':
            cur.execute(q('UPDATE clients SET name=?, phone=?, address=? WHERE id=? AND tenant_id=?', is_pg),
                        (request.form['name'], request.form['phone'],
                         request.form.get('address', ''), client_id, tenant_id))
            conn.commit()
            return redirect(url_for('inventory'))
        cur.execute(q('SELECT * FROM clients WHERE id = ? AND tenant_id = ?', is_pg), (client_id, tenant_id))
        client = dict(cur.fetchone())
    finally:
        conn.close()
    return render_template('edit_client.html', client=client)


@app.route('/delete_client/<int:client_id>', methods=['POST'])
@login_required
def delete_client(client_id):
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('DELETE FROM clients WHERE id = ? AND tenant_id = ?', is_pg), (client_id, tenant_id))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('inventory'))


# ── Financials ────────────────────────────────────────────────────────────────

@app.route('/credit_report')
@login_required
def credit_report():
    tenant_id = session['tenant_id']
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')

    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        where = 'tenant_id = ?'
        params = [tenant_id]
        if from_date:
            where += ' AND start_date >= ?'
            params.append(from_date)
        if to_date:
            where += ' AND start_date <= ?'
            params.append(to_date)

        cur.execute(q(f'SELECT * FROM rentals WHERE {where} ORDER BY id DESC', is_pg), params)
        rentals = rows(cur)

        rental_ids = [r['id'] for r in rentals]
        all_outside = []
        if rental_ids:
            ph = ','.join(['?' for _ in rental_ids])
            cur.execute(q(f'SELECT * FROM outside_items WHERE rental_id IN ({ph})', is_pg), rental_ids)
            all_outside = rows(cur)

        customer_total = sum(r['total_amount'] for r in rentals)
        customer_due = sum(r['balance'] for r in rentals)
        vendor_due = sum(oi['balance'] for oi in all_outside)
        vendor_total_cost = sum(oi['total'] for oi in all_outside)
        vendor_paid_total = sum(oi['paid'] for oi in all_outside)
        projected_net = customer_total - vendor_total_cost
        realized_net = sum(r['advance_paid'] for r in rentals) - vendor_paid_total

        # Group vendor items by (rental_id, vendor_name)
        rental_start_map = {r['id']: r['start_date'] for r in rentals}
        groups = defaultdict(list)
        for oi in all_outside:
            groups[(oi['rental_id'], oi['vendor_name'])].append(oi)

        vendors = []
        for (rid, vname), oi_list in groups.items():
            vendors.append({
                'rental_id': rid,
                'start_date': fmt_date(rental_start_map.get(rid, '')),
                'vendor_name': vname,
                'vendor_name_raw': vname,
                'items': [{'name': oi['item_name'], 'rate_per_day': oi['rate_per_day'],
                            'days': oi['days'], 'total': oi['total']} for oi in oi_list],
                'total': sum(oi['total'] for oi in oi_list),
                'paid': sum(oi['paid'] for oi in oi_list),
                'balance': sum(oi['balance'] for oi in oi_list),
            })

        if from_date and to_date:
            period_label = f"{fmt_date(from_date)} – {fmt_date(to_date)}"
        elif from_date:
            period_label = f"From {fmt_date(from_date)}"
        elif to_date:
            period_label = f"Until {fmt_date(to_date)}"
        else:
            period_label = 'All time'

        revenues = {
            'period_label': period_label,
            'customer_total': round(customer_total, 2),
            'customer_due': round(customer_due, 2),
            'vendor_due': round(vendor_due, 2),
            'projected_net': round(projected_net, 2),
            'realized_net': round(realized_net, 2),
        }
    finally:
        conn.close()

    return render_template('credit_report.html', rentals=rentals, vendors=vendors,
                           revenues=revenues, filters={'from_date': from_date, 'to_date': to_date})


@app.route('/add_payment/<int:rental_id>', methods=['POST'])
@login_required
def add_payment(rental_id):
    tenant_id = session['tenant_id']
    amount = float(request.form.get('payment', 0) or 0)
    if amount > 0:
        conn, is_pg = get_db()
        try:
            cur = conn.cursor()
            cur.execute(q('''UPDATE rentals SET advance_paid = advance_paid + ?, balance = balance - ?
                             WHERE id = ? AND tenant_id = ?''', is_pg),
                        (amount, amount, rental_id, tenant_id))
            conn.commit()
        finally:
            conn.close()
    return redirect(url_for('credit_report'))


@app.route('/pay_vendor_group/<int:rental_id>', methods=['POST'])
@login_required
def pay_vendor_group(rental_id):
    amount = float(request.form.get('payment', 0) or 0)
    vendor_name = request.form.get('vendor_name', '')
    if amount > 0:
        conn, is_pg = get_db()
        try:
            cur = conn.cursor()
            cur.execute(q('SELECT * FROM outside_items WHERE rental_id = ? AND vendor_name = ?', is_pg),
                        (rental_id, vendor_name))
            oi_list = rows(cur)
            group_total = sum(oi['total'] for oi in oi_list)
            if group_total > 0:
                for oi in oi_list:
                    share = round(amount * (oi['total'] / group_total), 2)
                    cur.execute(q('''UPDATE outside_items SET paid = paid + ?, balance = balance - ?
                                     WHERE id = ?''', is_pg), (share, share, oi['id']))
            conn.commit()
        finally:
            conn.close()
    return redirect(url_for('credit_report'))


@app.route('/vendor_dues')
@login_required
def vendor_dues():
    tenant_id = session['tenant_id']
    conn, is_pg = get_db()
    try:
        cur = conn.cursor()
        cur.execute(q('''SELECT oi.* FROM outside_items oi
                         JOIN rentals r ON oi.rental_id = r.id
                         WHERE oi.tenant_id = ?
                         ORDER BY oi.rental_id DESC''', is_pg), (tenant_id,))
        vendors = rows(cur)
    finally:
        conn.close()
    return render_template('vendor_dues.html', vendors=vendors)


@app.route('/pay_vendor/<int:vendor_id>', methods=['POST'])
@login_required
def pay_vendor(vendor_id):
    amount = float(request.form.get('payment', 0) or 0)
    if amount > 0:
        conn, is_pg = get_db()
        try:
            cur = conn.cursor()
            cur.execute(q('UPDATE outside_items SET paid = paid + ?, balance = balance - ? WHERE id = ?', is_pg),
                        (amount, amount, vendor_id))
            conn.commit()
        finally:
            conn.close()
    return redirect(url_for('vendor_dues'))


if __name__ == '__main__':
    app.run(debug=True)
