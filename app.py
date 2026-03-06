from flask import Flask, render_template, request, redirect, session, send_from_directory
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os


app = Flask(__name__)
app.secret_key = "super_secret_key_change_this"

DATABASE = "database.db"


# -----------------------
# Database connection
# -----------------------

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------
# Static files fix (Render)
# -----------------------

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


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


init_db()


# -----------------------
# Create owner
# -----------------------

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


create_owner()


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

    # INTERNAL ITEMS

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

    # OUTSIDE ITEMS

    vendor_names = request.form.getlist("vendor_name[]")
    vendor_rates = request.form.getlist("vendor_rates[]")

    for i in range(len(vendor_names)):

        if vendor_names[i] and vendor_rates[i]:

            vendor_total = days * float(vendor_rates[i])

            conn.execute("""
            INSERT INTO outside_items
            (rental_id,vendor_name,item_name,rate_per_day,
            days,total,paid,balance)
            VALUES(?,?,?,?,?,?,?,?)
            """, (
                rental_id,
                vendor_names[i],
                "Vendor Equipment",
                vendor_rates[i],
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
# Return item
# -----------------------

@app.route("/return_item/<int:id>")
def return_item(id):

    conn = get_db()

    item = conn.execute(
        "SELECT * FROM rental_items WHERE id=?",
        (id,)
    ).fetchone()

    conn.execute("""
    UPDATE rental_items
    SET status='Returned'
    WHERE id=?
    """,(id,))

    conn.execute("""
    UPDATE items
    SET status='Available'
    WHERE id=?
    """,(item["item_id"],))

    conn.commit()
    conn.close()

    return redirect("/rental_records")


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


# -----------------------
# Credit report
# -----------------------

@app.route("/credit_report")
def credit_report():

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    rentals = conn.execute("""
    SELECT * FROM rentals
    ORDER BY id DESC
    """).fetchall()

    vendors = conn.execute("""
    SELECT * FROM outside_items
    ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template(
        "credit_report.html",
        rentals=rentals,
        vendors=vendors
    )


# -----------------------
# Customer payment
# -----------------------

@app.route("/add_payment/<int:id>", methods=["POST"])
def add_payment(id):

    payment = float(request.form["payment"])

    conn = get_db()

    rental = conn.execute(
        "SELECT * FROM rentals WHERE id=?",
        (id,)
    ).fetchone()

    new_paid = rental["advance_paid"] + payment
    new_balance = rental["total_amount"] - new_paid

    conn.execute("""
    UPDATE rentals
    SET advance_paid=?, balance=?
    WHERE id=?
    """, (new_paid, new_balance, id))

    conn.commit()
    conn.close()

    return redirect("/credit_report")


# -----------------------
# Vendor payment
# -----------------------

@app.route("/pay_vendor/<int:id>", methods=["POST"])
def pay_vendor(id):

    payment = float(request.form["payment"])

    conn = get_db()

    vendor = conn.execute(
        "SELECT * FROM outside_items WHERE id=?",
        (id,)
    ).fetchone()

    new_paid = vendor["paid"] + payment
    new_balance = vendor["total"] - new_paid

    conn.execute("""
    UPDATE outside_items
    SET paid=?, balance=?
    WHERE id=?
    """, (new_paid, new_balance, id))

    conn.commit()
    conn.close()

    return redirect("/credit_report")


# -----------------------
# Run app
# -----------------------

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )