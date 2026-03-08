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
    conn = sqlite3.connect(DATABASE)
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

    conn.commit()
    conn.close()


def create_owner():

    conn = get_db()
    cursor = conn.cursor()

    owner = cursor.execute(
        "SELECT * FROM users WHERE role='owner'"
    ).fetchone()

    if not owner:

        password = generate_password_hash("admin123")

        cursor.execute("""
        INSERT INTO users(username,password,role)
        VALUES(?,?,?)
        """, ("owner", password, "owner"))

        conn.commit()

    conn.close()


def ensure_db_ready():
    global DB_READY
    if DB_READY:
        return
    init_db()
    create_owner()
    DB_READY = True


@app.before_request
def bootstrap_db():
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


@app.route("/healthz")
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
        rentals=rentals
    )


@app.route("/return_rental/<int:rental_id>", methods=["POST"])
def return_rental(rental_id):

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    conn.execute(
        "UPDATE rentals SET status='Returned' WHERE id=?",
        (rental_id,)
    )

    item_ids = conn.execute(
        "SELECT item_id FROM rental_items WHERE rental_id=?",
        (rental_id,)
    ).fetchall()

    for row in item_ids:
        conn.execute(
            "UPDATE items SET status='Available' WHERE id=?",
            (row["item_id"],)
        )

    conn.execute(
        "UPDATE rental_items SET status='Returned' WHERE rental_id=?",
        (rental_id,)
    )

    conn.commit()
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


# -----------------------
# Run app (local only)
# -----------------------

if __name__ == "__main__":
    app.run(debug=True)
