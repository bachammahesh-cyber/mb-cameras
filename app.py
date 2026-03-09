from flask import Flask, render_template, request, redirect, session
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = "super_secret_key_change_this"

DATABASE = "database.db"
DB_READY = False


# -----------------------
# Database connection
# -----------------------

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------
# Initialize database
# -----------------------

def init_db():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        category TEXT,
        rent_per_day REAL,
        status TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rentals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_name TEXT,
        phone TEXT,
        start_date TEXT,
        end_date TEXT,
        total_amount REAL,
        advance_paid REAL,
        balance REAL,
        status TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rental_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rental_id INTEGER,
        item_id INTEGER,
        rate_per_day REAL,
        days INTEGER,
        total REAL,
        status TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS outside_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rental_id INTEGER,
        vendor_name TEXT,
        item_name TEXT,
        rate_per_day REAL,
        days INTEGER,
        total REAL,
        paid REAL DEFAULT 0,
        balance REAL
    )
    """)

    # Backward-compatible migration for older DB files.
    rental_cols = [
        row["name"] for row in cursor.execute(
            "PRAGMA table_info(rental_items)"
        ).fetchall()
    ]
    if "status" not in rental_cols:
        cursor.execute(
            "ALTER TABLE rental_items ADD COLUMN status TEXT DEFAULT 'Active'"
        )

    outside_cols = [
        row["name"] for row in cursor.execute(
            "PRAGMA table_info(outside_items)"
        ).fetchall()
    ]
    if "paid" not in outside_cols:
        cursor.execute(
            "ALTER TABLE outside_items ADD COLUMN paid REAL DEFAULT 0"
        )
    if "balance" not in outside_cols:
        cursor.execute(
            "ALTER TABLE outside_items ADD COLUMN balance REAL"
        )
    cursor.execute("""
    UPDATE outside_items
    SET balance = COALESCE(balance, total - COALESCE(paid, 0))
    """)

    conn.commit()
    conn.close()


def seed_default_users():

    conn = get_db()
    cursor = conn.cursor()

    defaults = [
        ("maheshbacham", "aA@9440984550", "owner"),
        ("gopi", "9515369042", "manager")
    ]

    for username, raw_password, role in defaults:
        password = generate_password_hash(raw_password)

        cursor.execute("""
        INSERT INTO users(username,password,role)
        VALUES(?,?,?)
        ON CONFLICT(username) DO UPDATE SET
            password=excluded.password,
            role=excluded.role
        """, (username, password, role))

    conn.commit()
    conn.close()


def ensure_db_ready():
    global DB_READY
    if DB_READY:
        return
    init_db()
    seed_default_users()
    DB_READY = True


@app.before_request
def bootstrap_db():
    if request.endpoint in {"healthz", "static"}:
        return
    ensure_db_ready()


# -----------------------
# Login
# -----------------------

@app.route("/", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()

        user = conn.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()

        conn.close()

        if user and check_password_hash(user["password"], password):

            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]

            return redirect("/dashboard")

        return "Invalid login"

    return render_template("login.html")


@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200


# -----------------------
# Dashboard
# -----------------------

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect("/")

    return render_template(
        "dashboard.html",
        username=session["username"],
        role=session["role"]
    )


# -----------------------
# Logout
# -----------------------

@app.route("/logout")
def logout():

    session.clear()
    return redirect("/")


# -----------------------
# Inventory
# -----------------------

@app.route("/inventory")
def inventory():

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    items = conn.execute(
        "SELECT * FROM items"
    ).fetchall()

    conn.close()

    return render_template(
        "inventory.html",
        items=items,
        role=session["role"]
    )


# -----------------------
# Add item
# -----------------------

@app.route("/add_item", methods=["POST"])
def add_item():

    if "user_id" not in session:
        return redirect("/")

    name = request.form["name"]
    category = request.form["category"]
    rent = request.form["rent"]

    conn = get_db()

    conn.execute("""
    INSERT INTO items(name,category,rent_per_day,status)
    VALUES(?,?,?,?)
    """, (name, category, rent, "Available"))

    conn.commit()
    conn.close()

    return redirect("/inventory")


# -----------------------
# New rental
# -----------------------

@app.route("/new_rental")
def new_rental():

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    items = conn.execute(
        "SELECT * FROM items"
    ).fetchall()

    conn.close()

    return render_template(
        "new_rental.html",
        items=items
    )


# -----------------------
# Save rental
# -----------------------

@app.route("/save_rental", methods=["POST"])
def save_rental():

    if "user_id" not in session:
        return redirect("/")

    customer_name = request.form["customer_name"]
    phone = request.form["phone"]
    start_date = request.form["start_date"]
    end_date = request.form["end_date"]
    advance_paid = float(request.form["advance_paid"])

    d1 = datetime.strptime(start_date, "%Y-%m-%d")
    d2 = datetime.strptime(end_date, "%Y-%m-%d")

    days = (d2 - d1).days + 1

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO rentals
    (customer_name,phone,start_date,end_date,
    total_amount,advance_paid,balance,status)
    VALUES(?,?,?,?,?,?,?,?)
    """, (
        customer_name,
        phone,
        start_date,
        end_date,
        0,
        advance_paid,
        0,
        "Active"
    ))

    rental_id = cursor.lastrowid

    total_amount = 0

    item_ids = request.form.getlist("item_ids")

    for item_id in item_ids:

        item = conn.execute(
            "SELECT * FROM items WHERE id=?",
            (item_id,)
        ).fetchone()

        item_total = days * item["rent_per_day"]

        total_amount += item_total

        conn.execute("""
        INSERT INTO rental_items
        (rental_id,item_id,rate_per_day,days,total,status)
        VALUES(?,?,?,?,?,?)
        """, (
            rental_id,
            item_id,
            item["rent_per_day"],
            days,
            item_total,
            "Active"
        ))

        conn.execute(
            "UPDATE items SET status='Rented' WHERE id=?",
            (item_id,)
        )

    # Persist selected outside equipment as vendor dues.
    vendor_name = (
        request.form.get("vendor_name", "").strip()
        or next(
            (
                name.strip()
                for name in request.form.getlist("vendor_name[]")
                if name and name.strip()
            ),
            ""
        )
    )
    vendor_item_ids = request.form.getlist("vendor_item_ids")

    for item_id_text in vendor_item_ids:
        try:
            vendor_item_id = int(item_id_text)
        except ValueError:
            continue

        item = conn.execute(
            "SELECT name, rent_per_day FROM items WHERE id=?",
            (vendor_item_id,)
        ).fetchone()
        if not item:
            continue

        rate_text = request.form.get(f"vendor_rates[{vendor_item_id}]", "").strip()
        if rate_text:
            try:
                rate_per_day = float(rate_text)
            except ValueError:
                continue
            if rate_per_day <= 0:
                continue
        else:
            rate_per_day = item["rent_per_day"]

        vendor_total = days * rate_per_day

        conn.execute("""
        INSERT INTO outside_items
        (rental_id,vendor_name,item_name,rate_per_day,days,total,paid,balance)
        VALUES(?,?,?,?,?,?,?,?)
        """, (
            rental_id,
            vendor_name,
            item["name"],
            rate_per_day,
            days,
            vendor_total,
            0,
            vendor_total
        ))

    balance = total_amount - advance_paid

    conn.execute("""
    UPDATE rentals
    SET total_amount=?, balance=?
    WHERE id=?
    """, (total_amount, balance, rental_id))

    conn.commit()
    conn.close()

    return redirect("/dashboard")


# -----------------------
# Rental records
# -----------------------

@app.route("/rental_records")
def rental_records():

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    rentals = conn.execute(
        "SELECT * FROM rentals ORDER BY id DESC"
    ).fetchall()

    conn.close()

    return render_template(
        "rental_records.html",
        rentals=rentals,
        role=session["role"]
    )


@app.route("/toggle_rental_status/<int:rental_id>", methods=["POST"])
def toggle_rental_status(rental_id):

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()
    try:
        rental = conn.execute(
            "SELECT status FROM rentals WHERE id=?",
            (rental_id,)
        ).fetchone()

        if not rental:
            return redirect("/rental_records")

        # One-way transition: once returned, keep it returned.
        if rental["status"] != "Active":
            return redirect("/rental_records")

        new_status = "Returned"

        conn.execute(
            "UPDATE rentals SET status=? WHERE id=?",
            (new_status, rental_id)
        )

        item_ids = conn.execute(
            "SELECT item_id FROM rental_items WHERE rental_id=?",
            (rental_id,)
        ).fetchall()

        for row in item_ids:
            conn.execute(
                "UPDATE items SET status=? WHERE id=?",
                (
                    "Available",
                    row["item_id"]
                )
            )

        conn.execute(
            "UPDATE rental_items SET status=? WHERE rental_id=?",
            (new_status, rental_id)
        )

        conn.commit()
    finally:
        conn.close()

    return redirect("/rental_records")


@app.route("/edit_rental/<int:rental_id>", methods=["GET", "POST"])
def edit_rental(rental_id):

    if "user_id" not in session:
        return redirect("/")

    if session.get("role") != "owner":
        return redirect("/rental_records")

    conn = get_db()

    rental = conn.execute(
        "SELECT * FROM rentals WHERE id=?",
        (rental_id,)
    ).fetchone()

    if not rental:
        conn.close()
        return redirect("/rental_records")

    if request.method == "POST":

        customer_name = request.form["customer_name"]
        phone = request.form["phone"]
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        advance_paid = float(request.form["advance_paid"])
        status = request.form["status"]

        d1 = datetime.strptime(start_date, "%Y-%m-%d")
        d2 = datetime.strptime(end_date, "%Y-%m-%d")
        days = (d2 - d1).days + 1

        if days < 1:
            conn.close()
            return redirect(f"/edit_rental/{rental_id}")

        try:
            conn.execute("""
            UPDATE rentals
            SET customer_name=?, phone=?, start_date=?, end_date=?, advance_paid=?, status=?
            WHERE id=?
            """, (
                customer_name,
                phone,
                start_date,
                end_date,
                advance_paid,
                status,
                rental_id
            ))

            conn.execute("""
            UPDATE rental_items
            SET days=?, total=rate_per_day * ?, status=?
            WHERE rental_id=?
            """, (
                days,
                days,
                status,
                rental_id
            ))

            conn.execute("""
            UPDATE outside_items
            SET days=?, total=rate_per_day * ?, balance=(rate_per_day * ?) - paid
            WHERE rental_id=?
            """, (
                days,
                days,
                days,
                rental_id
            ))

            total_amount = conn.execute("""
            SELECT COALESCE(SUM(total), 0) AS total_amount
            FROM rental_items
            WHERE rental_id=?
            """, (rental_id,)).fetchone()["total_amount"]

            balance = total_amount - advance_paid

            conn.execute("""
            UPDATE rentals
            SET total_amount=?, balance=?
            WHERE id=?
            """, (total_amount, balance, rental_id))

            item_ids = conn.execute(
                "SELECT item_id FROM rental_items WHERE rental_id=?",
                (rental_id,)
            ).fetchall()

            for row in item_ids:
                conn.execute(
                    "UPDATE items SET status=? WHERE id=?",
                    ("Rented" if status == "Active" else "Available", row["item_id"])
                )

            conn.commit()
        finally:
            conn.close()

        return redirect("/rental_records")

    conn.close()
    return render_template("edit_rental.html", rental=rental)


@app.route("/delete_rental/<int:rental_id>", methods=["POST"])
def delete_rental(rental_id):

    if "user_id" not in session:
        return redirect("/")

    if session.get("role") != "owner":
        return redirect("/rental_records")

    conn = get_db()
    try:
        item_ids = conn.execute(
            "SELECT item_id FROM rental_items WHERE rental_id=?",
            (rental_id,)
        ).fetchall()

        for row in item_ids:
            conn.execute(
                "UPDATE items SET status='Available' WHERE id=?",
                (row["item_id"],)
            )

        conn.execute("DELETE FROM rental_items WHERE rental_id=?", (rental_id,))
        conn.execute("DELETE FROM outside_items WHERE rental_id=?", (rental_id,))
        conn.execute("DELETE FROM rentals WHERE id=?", (rental_id,))
        conn.commit()
    finally:
        conn.close()

    return redirect("/rental_records")


# -----------------------
# Credit report
# -----------------------

@app.route("/credit_report")
def credit_report():

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    rentals = conn.execute(
        "SELECT * FROM rentals ORDER BY id DESC"
    ).fetchall()

    vendors = conn.execute(
        "SELECT * FROM outside_items ORDER BY id DESC"
    ).fetchall()

    conn.close()

    return render_template(
        "credit_report.html",
        rentals=rentals,
        vendors=vendors
    )


@app.route("/add_payment/<int:rental_id>", methods=["POST"])
def add_payment(rental_id):

    if "user_id" not in session:
        return redirect("/")

    payment_text = request.form.get("payment", "").strip()
    try:
        payment = float(payment_text)
    except ValueError:
        return redirect("/credit_report")

    if payment <= 0:
        return redirect("/credit_report")

    conn = get_db()
    try:
        rental = conn.execute(
            "SELECT total_amount, advance_paid FROM rentals WHERE id=?",
            (rental_id,)
        ).fetchone()

        if not rental:
            return redirect("/credit_report")

        updated_paid = min(rental["total_amount"], rental["advance_paid"] + payment)
        balance = rental["total_amount"] - updated_paid

        conn.execute(
            "UPDATE rentals SET advance_paid=?, balance=? WHERE id=?",
            (updated_paid, balance, rental_id)
        )
        conn.commit()
    finally:
        conn.close()

    return redirect("/credit_report")


@app.route("/pay_vendor/<int:vendor_id>", methods=["POST"])
def pay_vendor(vendor_id):

    if "user_id" not in session:
        return redirect("/")

    payment_text = request.form.get("payment", "").strip()
    try:
        payment = float(payment_text)
    except ValueError:
        return redirect("/credit_report")

    if payment <= 0:
        return redirect("/credit_report")

    conn = get_db()
    try:
        vendor = conn.execute(
            "SELECT total, paid FROM outside_items WHERE id=?",
            (vendor_id,)
        ).fetchone()

        if not vendor:
            return redirect("/credit_report")

        updated_paid = min(vendor["total"], (vendor["paid"] or 0) + payment)
        balance = vendor["total"] - updated_paid

        conn.execute(
            "UPDATE outside_items SET paid=?, balance=? WHERE id=?",
            (updated_paid, balance, vendor_id)
        )
        conn.commit()
    finally:
        conn.close()

    return redirect("/credit_report")


# -----------------------
# Run app (local only)
# -----------------------

if __name__ == "__main__":
    app.run(debug=True)
