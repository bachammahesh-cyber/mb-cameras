from flask import Flask, render_template, request, redirect, session
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

app = Flask(__name__)
app.secret_key = "super_secret_key_change_this"

DEFAULT_DATABASE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "database.db"
)
DATABASE = os.getenv("DATABASE_PATH", DEFAULT_DATABASE)
DATABASE_URL = (
    os.getenv("DATABASE_URL", "").strip()
    or os.getenv("NEON_DATABASE_URL", "").strip()
)
if DATABASE_URL.startswith(("'", '"')) and DATABASE_URL.endswith(("'", '"')):
    DATABASE_URL = DATABASE_URL[1:-1].strip()

USE_POSTGRES = bool(DATABASE_URL)
IS_RENDER = bool(
    os.getenv("RENDER")
    or os.getenv("RENDER_SERVICE_ID")
    or os.getenv("RENDER_EXTERNAL_URL")
)
DB_READY = False

if IS_RENDER and not USE_POSTGRES:
    raise RuntimeError(
        "Render deployment requires Neon Postgres. Set DATABASE_URL to your Neon connection string."
    )


# -----------------------
# Database connection
# -----------------------

class PostgresConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, query, params=()):
        return self._conn.execute(query.replace("?", "%s"), params)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


def get_db():
    if USE_POSTGRES:
        if psycopg is None or dict_row is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed.")
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return PostgresConnection(conn)

    db_dir = os.path.dirname(os.path.abspath(DATABASE))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------
# Initialize database
# -----------------------

def init_db():

    conn = get_db()
    if USE_POSTGRES:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id BIGSERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS items(
            id BIGSERIAL PRIMARY KEY,
            name TEXT,
            category TEXT,
            rent_per_day DOUBLE PRECISION,
            status TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rentals(
            id BIGSERIAL PRIMARY KEY,
            customer_name TEXT,
            phone TEXT,
            start_date TEXT,
            end_date TEXT,
            total_amount DOUBLE PRECISION,
            advance_paid DOUBLE PRECISION,
            balance DOUBLE PRECISION,
            status TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rental_items(
            id BIGSERIAL PRIMARY KEY,
            rental_id BIGINT,
            item_id BIGINT,
            rate_per_day DOUBLE PRECISION,
            days INTEGER,
            total DOUBLE PRECISION,
            status TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS outside_items(
            id BIGSERIAL PRIMARY KEY,
            rental_id BIGINT,
            vendor_name TEXT,
            item_name TEXT,
            rate_per_day DOUBLE PRECISION,
            days INTEGER,
            total DOUBLE PRECISION,
            paid DOUBLE PRECISION DEFAULT 0,
            balance DOUBLE PRECISION
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS clients(
            id BIGSERIAL PRIMARY KEY,
            name TEXT,
            phone TEXT,
            address TEXT
        )
        """)

        rental_status_exists = conn.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='rental_items' AND column_name='status'
        """).fetchone()
        if not rental_status_exists:
            conn.execute(
                "ALTER TABLE rental_items ADD COLUMN status TEXT DEFAULT 'Active'"
            )

        outside_paid_exists = conn.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='outside_items' AND column_name='paid'
        """).fetchone()
        if not outside_paid_exists:
            conn.execute(
                "ALTER TABLE outside_items ADD COLUMN paid DOUBLE PRECISION DEFAULT 0"
            )

        outside_balance_exists = conn.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='outside_items' AND column_name='balance'
        """).fetchone()
        if not outside_balance_exists:
            conn.execute(
                "ALTER TABLE outside_items ADD COLUMN balance DOUBLE PRECISION"
            )
    else:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            category TEXT,
            rent_per_day REAL,
            status TEXT
        )
        """)

        conn.execute("""
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

        conn.execute("""
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

        conn.execute("""
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

        conn.execute("""
        CREATE TABLE IF NOT EXISTS clients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            address TEXT
        )
        """)

        # Backward-compatible migration for older SQLite DB files.
        rental_cols = [
            row["name"] for row in conn.execute(
                "PRAGMA table_info(rental_items)"
            ).fetchall()
        ]
        if "status" not in rental_cols:
            conn.execute(
                "ALTER TABLE rental_items ADD COLUMN status TEXT DEFAULT 'Active'"
            )

        outside_cols = [
            row["name"] for row in conn.execute(
                "PRAGMA table_info(outside_items)"
            ).fetchall()
        ]
        if "paid" not in outside_cols:
            conn.execute(
                "ALTER TABLE outside_items ADD COLUMN paid REAL DEFAULT 0"
            )
        if "balance" not in outside_cols:
            conn.execute(
                "ALTER TABLE outside_items ADD COLUMN balance REAL"
            )

    conn.execute("""
    UPDATE outside_items
    SET balance = COALESCE(balance, total - COALESCE(paid, 0))
    """)

    conn.commit()
    conn.close()


def seed_default_users():

    conn = get_db()

    defaults = [
        ("maheshbacham", "aA@9440984550", "owner"),
        ("gopi", "9515369042", "manager")
    ]

    for username, raw_password, role in defaults:
        password = generate_password_hash(raw_password)

        conn.execute("""
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

    item_rows = conn.execute(
        "SELECT * FROM items ORDER BY id DESC"
    ).fetchall()

    item_usage = {
        row["item_id"]: row["usage_count"]
        for row in conn.execute("""
        SELECT item_id, COUNT(*) AS usage_count
        FROM rental_items
        GROUP BY item_id
        """).fetchall()
    }

    clients = conn.execute(
        "SELECT * FROM clients ORDER BY id DESC"
    ).fetchall()

    conn.close()

    items = []
    for row in item_rows:
        usage_count = item_usage.get(row["id"], 0)
        can_delete = row["status"] != "Rented" and usage_count == 0
        delete_reason = ""
        if row["status"] == "Rented":
            delete_reason = "Item is currently rented."
        elif usage_count > 0:
            delete_reason = "Item has rental history and cannot be deleted."

        items.append({
            **dict(row),
            "can_delete": can_delete,
            "delete_reason": delete_reason
        })

    return render_template(
        "inventory.html",
        items=items,
        clients=clients,
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


@app.route("/edit_item/<int:item_id>", methods=["GET", "POST"])
def edit_item(item_id):

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    item = conn.execute(
        "SELECT * FROM items WHERE id=?",
        (item_id,)
    ).fetchone()

    if not item:
        conn.close()
        return redirect("/inventory")

    if request.method == "POST":
        name = request.form["name"]
        category = request.form["category"]
        rent = request.form["rent"]

        conn.execute("""
        UPDATE items
        SET name=?, category=?, rent_per_day=?
        WHERE id=?
        """, (name, category, rent, item_id))

        conn.commit()
        conn.close()
        return redirect("/inventory")

    conn.close()
    return render_template("edit_item.html", item=item)


@app.route("/delete_item/<int:item_id>", methods=["POST"])
def delete_item(item_id):

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()
    try:
        item = conn.execute(
            "SELECT status FROM items WHERE id=?",
            (item_id,)
        ).fetchone()
        if not item:
            return redirect("/inventory")

        item_usage = conn.execute(
            "SELECT COUNT(*) AS usage_count FROM rental_items WHERE item_id=?",
            (item_id,)
        ).fetchone()["usage_count"]

        if item["status"] == "Rented" or item_usage > 0:
            return redirect("/inventory")

        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.commit()
    finally:
        conn.close()

    return redirect("/inventory")


# -----------------------
# Add client
# -----------------------

@app.route("/add_client", methods=["POST"])
def add_client():

    if "user_id" not in session:
        return redirect("/")

    name = request.form["name"]
    phone = request.form["phone"]
    address = request.form["address"]

    conn = get_db()

    conn.execute("""
    INSERT INTO clients(name,phone,address)
    VALUES(?,?,?)
    """, (name, phone, address))

    conn.commit()
    conn.close()

    return redirect("/inventory")


@app.route("/edit_client/<int:client_id>", methods=["GET", "POST"])
def edit_client(client_id):

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()

    client = conn.execute(
        "SELECT * FROM clients WHERE id=?",
        (client_id,)
    ).fetchone()

    if not client:
        conn.close()
        return redirect("/inventory")

    if request.method == "POST":
        name = request.form["name"]
        phone = request.form["phone"]
        address = request.form["address"]

        conn.execute("""
        UPDATE clients
        SET name=?, phone=?, address=?
        WHERE id=?
        """, (name, phone, address, client_id))

        conn.commit()
        conn.close()
        return redirect("/inventory")

    conn.close()
    return render_template("edit_client.html", client=client)


@app.route("/delete_client/<int:client_id>", methods=["POST"])
def delete_client(client_id):

    if "user_id" not in session:
        return redirect("/")

    conn = get_db()
    try:
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
        conn.commit()
    finally:
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

    clients = conn.execute(
        "SELECT * FROM clients ORDER BY name ASC, id DESC"
    ).fetchall()

    conn.close()

    return render_template(
        "new_rental.html",
        items=items,
        clients=clients
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

    if USE_POSTGRES:
        rental_id = conn.execute("""
        INSERT INTO rentals
        (customer_name,phone,start_date,end_date,
        total_amount,advance_paid,balance,status)
        VALUES(?,?,?,?,?,?,?,?)
        RETURNING id
        """, (
            customer_name,
            phone,
            start_date,
            end_date,
            0,
            advance_paid,
            0,
            "Active"
        )).fetchone()["id"]
    else:
        conn.execute("""
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
        rental_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

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

    rental_rows = conn.execute(
        "SELECT * FROM rentals ORDER BY start_date DESC, id DESC"
    ).fetchall()

    rental_item_rows = conn.execute("""
    SELECT ri.rental_id, i.name, ri.rate_per_day
    FROM rental_items ri
    JOIN items i ON i.id = ri.item_id
    ORDER BY ri.rental_id DESC, i.name ASC
    """).fetchall()

    conn.close()

    equipment_by_rental = {}
    for row in rental_item_rows:
        equipment_by_rental.setdefault(row["rental_id"], []).append(
            f"{row['name']} - {row['rate_per_day']}"
        )

    rentals = []
    for row in rental_rows:
        start_date = row["start_date"]
        end_date = row["end_date"]
        rental_days = 0
        given_date_display = start_date

        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            rental_days = (end_dt - start_dt).days + 1
            if rental_days < 0:
                rental_days = 0
            given_date_display = start_dt.strftime("%d/%m/%y")
        except (TypeError, ValueError):
            pass

        rentals.append({
            **dict(row),
            "equipment_names": equipment_by_rental.get(row["id"], []),
            "rental_days": rental_days,
            "given_date_display": given_date_display
        })

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
            previous_item_rows = conn.execute(
                "SELECT item_id FROM rental_items WHERE rental_id=?",
                (rental_id,)
            ).fetchall()
            previous_item_ids = [row["item_id"] for row in previous_item_rows]
            previous_outside_rows = conn.execute("""
            SELECT item_name, paid
            FROM outside_items
            WHERE rental_id=?
            """, (rental_id,)).fetchall()
            previous_outside_payments = {
                row["item_name"]: row["paid"] or 0 for row in previous_outside_rows
            }

            selected_item_ids = []
            for item_id_text in request.form.getlist("item_ids"):
                try:
                    item_id = int(item_id_text)
                except ValueError:
                    continue
                if item_id not in selected_item_ids:
                    selected_item_ids.append(item_id)

            item_lookup = {}
            if selected_item_ids:
                placeholders = ",".join("?" for _ in selected_item_ids)
                selected_items = conn.execute(
                    f"SELECT * FROM items WHERE id IN ({placeholders})",
                    tuple(selected_item_ids)
                ).fetchall()
                item_lookup = {row["id"]: row for row in selected_items}

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
            vendor_item_ids = []
            for item_id_text in request.form.getlist("vendor_item_ids"):
                try:
                    vendor_item_id = int(item_id_text)
                except ValueError:
                    continue
                if vendor_item_id not in vendor_item_ids:
                    vendor_item_ids.append(vendor_item_id)

            vendor_item_lookup = {}
            if vendor_item_ids:
                vendor_placeholders = ",".join("?" for _ in vendor_item_ids)
                vendor_items = conn.execute(
                    f"SELECT id, name, rent_per_day FROM items WHERE id IN ({vendor_placeholders})",
                    tuple(vendor_item_ids)
                ).fetchall()
                vendor_item_lookup = {row["id"]: row for row in vendor_items}

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

            for item_id in previous_item_ids:
                conn.execute(
                    "UPDATE items SET status='Available' WHERE id=?",
                    (item_id,)
                )

            conn.execute(
                "DELETE FROM rental_items WHERE rental_id=?",
                (rental_id,)
            )

            total_amount = 0
            for item_id in selected_item_ids:
                item = item_lookup.get(item_id)
                if not item:
                    continue
                if item["status"] != "Available" and item_id not in previous_item_ids:
                    continue

                rate_text = request.form.get(f"item_rates[{item_id}]", "").strip()
                if rate_text:
                    try:
                        rate_per_day = float(rate_text)
                    except ValueError:
                        rate_per_day = item["rent_per_day"]
                else:
                    rate_per_day = item["rent_per_day"]

                if rate_per_day < 0:
                    rate_per_day = item["rent_per_day"]

                item_total = rate_per_day * days
                total_amount += item_total

                conn.execute("""
                INSERT INTO rental_items
                (rental_id,item_id,rate_per_day,days,total,status)
                VALUES(?,?,?,?,?,?)
                """, (
                    rental_id,
                    item_id,
                    rate_per_day,
                    days,
                    item_total,
                    status
                ))

                conn.execute(
                    "UPDATE items SET status=? WHERE id=?",
                    ("Rented" if status == "Active" else "Available", item_id)
                )

            conn.execute(
                "DELETE FROM outside_items WHERE rental_id=?",
                (rental_id,)
            )

            for vendor_item_id in vendor_item_ids:
                item = vendor_item_lookup.get(vendor_item_id)
                if not item:
                    continue

                rate_text = request.form.get(f"vendor_rates[{vendor_item_id}]", "").strip()
                if rate_text:
                    try:
                        rate_per_day = float(rate_text)
                    except ValueError:
                        rate_per_day = item["rent_per_day"]
                else:
                    rate_per_day = item["rent_per_day"]

                if rate_per_day <= 0:
                    rate_per_day = item["rent_per_day"]

                vendor_total = rate_per_day * days
                paid = min(previous_outside_payments.get(item["name"], 0), vendor_total)
                balance_due = vendor_total - paid

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
                    paid,
                    balance_due
                ))

            balance = total_amount - advance_paid

            conn.execute("""
            UPDATE rentals
            SET total_amount=?, balance=?
            WHERE id=?
            """, (total_amount, balance, rental_id))

            conn.commit()
        finally:
            conn.close()

        return redirect("/rental_records")

    clients = conn.execute(
        "SELECT * FROM clients ORDER BY name ASC, id DESC"
    ).fetchall()

    current_rental_items = conn.execute("""
    SELECT item_id, rate_per_day
    FROM rental_items
    WHERE rental_id=?
    """, (rental_id,)).fetchall()
    current_outside_items = conn.execute("""
    SELECT vendor_name, item_name, rate_per_day
    FROM outside_items
    WHERE rental_id=?
    ORDER BY id ASC
    """, (rental_id,)).fetchall()

    selected_item_ids = [row["item_id"] for row in current_rental_items]
    selected_rates = {
        row["item_id"]: row["rate_per_day"] for row in current_rental_items
    }
    outside_vendor_name = next(
        (
            (row["vendor_name"] or "").strip()
            for row in current_outside_items
            if row["vendor_name"] and row["vendor_name"].strip()
        ),
        ""
    )
    selected_vendor_item_names = [row["item_name"] for row in current_outside_items]
    selected_vendor_rates = {
        row["item_name"]: row["rate_per_day"] for row in current_outside_items
    }

    if selected_item_ids:
        placeholders = ",".join("?" for _ in selected_item_ids)
        items = conn.execute(
            f"""
            SELECT *
            FROM items
            WHERE status='Available' OR id IN ({placeholders})
            ORDER BY name ASC, id DESC
            """,
            tuple(selected_item_ids)
        ).fetchall()
    else:
        items = conn.execute("""
        SELECT *
        FROM items
        WHERE status='Available'
        ORDER BY name ASC, id DESC
        """).fetchall()

    vendor_items = conn.execute("""
    SELECT id, name, rent_per_day
    FROM items
    ORDER BY name ASC, id DESC
    """).fetchall()

    conn.close()
    return render_template(
        "edit_rental.html",
        rental=rental,
        clients=clients,
        items=items,
        vendor_items=vendor_items,
        selected_item_ids=selected_item_ids,
        selected_rates=selected_rates,
        outside_vendor_name=outside_vendor_name,
        selected_vendor_item_names=selected_vendor_item_names,
        selected_vendor_rates=selected_vendor_rates
    )


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

    month = request.args.get("month", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()

    selected_month = ""
    period_label = "All time"

    if month:
        try:
            month_start = datetime.strptime(month, "%Y-%m")
            next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_end = next_month - timedelta(days=1)
            from_date = month_start.strftime("%Y-%m-%d")
            to_date = month_end.strftime("%Y-%m-%d")
            selected_month = month
            period_label = month_start.strftime("%B %Y")
        except ValueError:
            month = ""

    parsed_from = None
    parsed_to = None
    if from_date:
        try:
            parsed_from = datetime.strptime(from_date, "%Y-%m-%d")
        except ValueError:
            from_date = ""
    if to_date:
        try:
            parsed_to = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            to_date = ""

    if parsed_from and parsed_to and parsed_from > parsed_to:
        from_date, to_date = to_date, from_date
        parsed_from, parsed_to = parsed_to, parsed_from

    if not month and from_date and to_date:
        period_label = f"{from_date} to {to_date}"
    elif not month and from_date:
        period_label = f"From {from_date}"
    elif not month and to_date:
        period_label = f"Up to {to_date}"

    rental_where = []
    rental_params = []
    if from_date:
        rental_where.append("start_date >= ?")
        rental_params.append(from_date)
    if to_date:
        rental_where.append("start_date <= ?")
        rental_params.append(to_date)

    rental_where_sql = ""
    if rental_where:
        rental_where_sql = "WHERE " + " AND ".join(rental_where)

    conn = get_db()

    rentals = conn.execute(
        f"SELECT * FROM rentals {rental_where_sql} ORDER BY start_date DESC, id DESC",
        tuple(rental_params)
    ).fetchall()

    vendor_where = []
    vendor_params = []
    if from_date:
        vendor_where.append("r.start_date >= ?")
        vendor_params.append(from_date)
    if to_date:
        vendor_where.append("r.start_date <= ?")
        vendor_params.append(to_date)

    vendor_where_sql = ""
    if vendor_where:
        vendor_where_sql = "WHERE " + " AND ".join(vendor_where)

    vendor_rows = conn.execute(
        f"""
        SELECT oi.*, r.start_date
        FROM outside_items oi
        JOIN rentals r ON r.id = oi.rental_id
        {vendor_where_sql}
        ORDER BY r.start_date DESC, oi.id DESC
        """,
        tuple(vendor_params)
    ).fetchall()

    conn.close()

    grouped_vendors = []
    groups = {}
    for row in vendor_rows:
        vendor_name = (row["vendor_name"] or "").strip()
        key = (row["rental_id"], vendor_name)

        if key not in groups:
            groups[key] = {
                "rental_id": row["rental_id"],
                "start_date": row["start_date"],
                "vendor_name_raw": vendor_name,
                "vendor_name": vendor_name or "Outside Vendor",
                "total": 0,
                "paid": 0,
                "balance": 0,
                "items": []
            }
            grouped_vendors.append(groups[key])

        group = groups[key]
        row_total = row["total"] or 0
        row_paid = row["paid"] or 0
        row_balance = row["balance"]
        if row_balance is None:
            row_balance = row_total - row_paid

        group["total"] += row_total
        group["paid"] += row_paid
        group["balance"] += row_balance
        group["items"].append({
            "name": row["item_name"],
            "rate_per_day": row["rate_per_day"],
            "days": row["days"],
            "total": row_total
        })

    customer_total = sum((r["total_amount"] or 0) for r in rentals)
    customer_paid = sum((r["advance_paid"] or 0) for r in rentals)
    customer_due = sum((r["balance"] or 0) for r in rentals)
    vendor_total = sum((v["total"] or 0) for v in grouped_vendors)
    vendor_paid = sum((v["paid"] or 0) for v in grouped_vendors)
    vendor_due = sum((v["balance"] or 0) for v in grouped_vendors)

    revenues = {
        "period_label": period_label,
        "customer_total": customer_total,
        "customer_paid": customer_paid,
        "customer_due": customer_due,
        "vendor_total": vendor_total,
        "vendor_paid": vendor_paid,
        "vendor_due": vendor_due,
        "projected_net": customer_total - vendor_total,
        "realized_net": customer_paid - vendor_paid
    }

    return render_template(
        "credit_report.html",
        rentals=rentals,
        vendors=grouped_vendors,
        revenues=revenues,
        filters={
            "month": selected_month,
            "from_date": from_date,
            "to_date": to_date
        }
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


@app.route("/pay_vendor_group/<int:rental_id>", methods=["POST"])
def pay_vendor_group(rental_id):

    if "user_id" not in session:
        return redirect("/")

    payment_text = request.form.get("payment", "").strip()
    vendor_name = request.form.get("vendor_name", "").strip()
    try:
        payment = float(payment_text)
    except ValueError:
        return redirect("/credit_report")

    if payment <= 0:
        return redirect("/credit_report")

    conn = get_db()
    try:
        vendor_rows = conn.execute("""
        SELECT id,total,COALESCE(paid,0) AS paid,COALESCE(balance,total-COALESCE(paid,0)) AS balance
        FROM outside_items
        WHERE rental_id=? AND COALESCE(vendor_name,'')=?
        ORDER BY id ASC
        """, (rental_id, vendor_name)).fetchall()

        if not vendor_rows:
            return redirect("/credit_report")

        remaining_due = sum(row["balance"] for row in vendor_rows)
        payment_left = min(payment, remaining_due)

        for row in vendor_rows:
            if payment_left <= 0:
                break
            if row["balance"] <= 0:
                continue

            row_payment = min(row["balance"], payment_left)
            updated_paid = row["paid"] + row_payment
            updated_balance = row["total"] - updated_paid

            conn.execute(
                "UPDATE outside_items SET paid=?, balance=? WHERE id=?",
                (updated_paid, updated_balance, row["id"])
            )
            payment_left -= row_payment

        conn.commit()
    finally:
        conn.close()

    return redirect("/credit_report")


# -----------------------
# Run app (local only)
# -----------------------

if __name__ == "__main__":
    app.run(debug=True)
