from flask import Flask, render_template, request, redirect, session, send_from_directory
from urllib.parse import quote_plus
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
app.secret_key = os.getenv("SECRET_KEY", "dev_secret_key_change_this")
app.jinja_env.filters['urlencode'] = quote_plus
app.jinja_env.filters['enumerate'] = enumerate

def fmt_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").strftime("%d/%m/%y")
    except (TypeError, ValueError):
        return s or ""
app.jinja_env.filters['fmt_date'] = fmt_date

PRIMARY_TENANT_ID = "mb_cameras"
DEMO_TENANT_ID = "demo_rental_house"
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "Demo@123"

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

if IS_RENDER and not os.getenv("SECRET_KEY", "").strip():
    raise RuntimeError(
        "Render deployment requires SECRET_KEY. Set it to a long random string in Render environment variables."
    )

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_RENDER


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


def current_tenant_id():
    return session.get("tenant_id", PRIMARY_TENANT_ID)


def tenant_demo_data_exists(conn, tenant_id):
    existing_item = conn.execute(
        "SELECT id FROM items WHERE tenant_id=? LIMIT 1",
        (tenant_id,)
    ).fetchone()
    if existing_item:
        return True

    existing_client = conn.execute(
        "SELECT id FROM clients WHERE tenant_id=? LIMIT 1",
        (tenant_id,)
    ).fetchone()
    return bool(existing_client)


def get_user_by_username(conn, username):
    normalized_username = username.strip().lower()
    return conn.execute(
        "SELECT * FROM users WHERE LOWER(TRIM(username))=?",
        (normalized_username,)
    ).fetchone()


def upsert_user(conn, username, raw_password, role, tenant_id):
    normalized_username = username.strip().lower()
    password = generate_password_hash(raw_password)
    existing_user = get_user_by_username(conn, normalized_username)

    if existing_user:
        conn.execute("""
        UPDATE users
        SET username=?, password=?, role=?, tenant_id=?
        WHERE id=?
        """, (
            normalized_username,
            password,
            role,
            tenant_id,
            existing_user["id"]
        ))
        return

    conn.execute("""
    INSERT INTO users(username,password,role,tenant_id)
    VALUES(?,?,?,?)
    """, (normalized_username, password, role, tenant_id))


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
            role TEXT,
            tenant_id TEXT DEFAULT 'mb_cameras'
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS items(
            id BIGSERIAL PRIMARY KEY,
            tenant_id TEXT DEFAULT 'mb_cameras',
            name TEXT,
            category TEXT,
            rent_per_day DOUBLE PRECISION,
            status TEXT,
            custom_id TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rentals(
            id BIGSERIAL PRIMARY KEY,
            tenant_id TEXT DEFAULT 'mb_cameras',
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
            tenant_id TEXT DEFAULT 'mb_cameras',
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
            tenant_id TEXT DEFAULT 'mb_cameras',
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
            tenant_id TEXT DEFAULT 'mb_cameras',
            name TEXT,
            phone TEXT,
            address TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices(
            id BIGSERIAL PRIMARY KEY,
            tenant_id TEXT DEFAULT 'mb_cameras',
            rental_id BIGINT,
            invoice_number TEXT,
            invoice_date TEXT,
            gst_type TEXT,
            customer_gstin TEXT,
            customer_gst_name TEXT,
            created_at TEXT
        )
        """)

        postgres_tenant_migrations = {
            "users": "ALTER TABLE users ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "items": "ALTER TABLE items ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "rentals": "ALTER TABLE rentals ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "rental_items": "ALTER TABLE rental_items ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "outside_items": "ALTER TABLE outside_items ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "clients": "ALTER TABLE clients ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
        }
        for table_name, statement in postgres_tenant_migrations.items():
            tenant_exists = conn.execute("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name=%s AND column_name='tenant_id'
            """, (table_name,)).fetchone()
            if not tenant_exists:
                conn.execute(statement)

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

        rental_items_qty_exists = conn.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='rental_items' AND column_name='quantity'
        """).fetchone()
        if not rental_items_qty_exists:
            conn.execute(
                "ALTER TABLE rental_items ADD COLUMN quantity INTEGER DEFAULT 1"
            )

        outside_items_qty_exists = conn.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='outside_items' AND column_name='quantity'
        """).fetchone()
        if not outside_items_qty_exists:
            conn.execute(
                "ALTER TABLE outside_items ADD COLUMN quantity INTEGER DEFAULT 1"
            )

        items_cid_exists = conn.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='items' AND column_name='custom_id'
        """).fetchone()
        if not items_cid_exists:
            conn.execute("ALTER TABLE items ADD COLUMN custom_id TEXT")

        inv_gst_name_exists = conn.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='invoices' AND column_name='customer_gst_name'
        """).fetchone()
        if not inv_gst_name_exists:
            conn.execute("ALTER TABLE invoices ADD COLUMN customer_gst_name TEXT")
    else:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT,
            tenant_id TEXT DEFAULT 'mb_cameras'
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT DEFAULT 'mb_cameras',
            name TEXT,
            category TEXT,
            rent_per_day REAL,
            status TEXT,
            custom_id TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rentals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT DEFAULT 'mb_cameras',
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
            tenant_id TEXT DEFAULT 'mb_cameras',
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
            tenant_id TEXT DEFAULT 'mb_cameras',
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
            tenant_id TEXT DEFAULT 'mb_cameras',
            name TEXT,
            phone TEXT,
            address TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT DEFAULT 'mb_cameras',
            rental_id INTEGER,
            invoice_number TEXT,
            invoice_date TEXT,
            gst_type TEXT,
            customer_gstin TEXT,
            customer_gst_name TEXT,
            created_at TEXT
        )
        """)

        sqlite_tenant_migrations = {
            "users": "ALTER TABLE users ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "items": "ALTER TABLE items ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "rentals": "ALTER TABLE rentals ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "rental_items": "ALTER TABLE rental_items ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "outside_items": "ALTER TABLE outside_items ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
            "clients": "ALTER TABLE clients ADD COLUMN tenant_id TEXT DEFAULT 'mb_cameras'",
        }
        for table_name, statement in sqlite_tenant_migrations.items():
            table_cols = [
                row["name"] for row in conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            ]
            if "tenant_id" not in table_cols:
                conn.execute(statement)

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

        rental_cols2 = [
            row["name"] for row in conn.execute(
                "PRAGMA table_info(rental_items)"
            ).fetchall()
        ]
        if "quantity" not in rental_cols2:
            conn.execute(
                "ALTER TABLE rental_items ADD COLUMN quantity INTEGER DEFAULT 1"
            )

        outside_cols2 = [
            row["name"] for row in conn.execute(
                "PRAGMA table_info(outside_items)"
            ).fetchall()
        ]
        if "quantity" not in outside_cols2:
            conn.execute(
                "ALTER TABLE outside_items ADD COLUMN quantity INTEGER DEFAULT 1"
            )

        items_cols = [
            row["name"] for row in conn.execute(
                "PRAGMA table_info(items)"
            ).fetchall()
        ]
        if "custom_id" not in items_cols:
            conn.execute("ALTER TABLE items ADD COLUMN custom_id TEXT")

        inv_cols = [
            row["name"] for row in conn.execute(
                "PRAGMA table_info(invoices)"
            ).fetchall()
        ]
        if "customer_gst_name" not in inv_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN customer_gst_name TEXT")

    conn.execute("""
    UPDATE outside_items
    SET balance = COALESCE(balance, total - COALESCE(paid, 0))
    """)

    conn.execute("""
    UPDATE users
    SET tenant_id = COALESCE(NULLIF(tenant_id, ''), ?)
    """, (PRIMARY_TENANT_ID,))

    conn.execute("""
    UPDATE users
    SET tenant_id = ?
    WHERE username = ?
    """, (DEMO_TENANT_ID, DEMO_USERNAME))

    conn.execute("""
    UPDATE items
    SET tenant_id = COALESCE(NULLIF(tenant_id, ''), ?)
    """, (PRIMARY_TENANT_ID,))

    conn.execute("""
    UPDATE rentals
    SET tenant_id = COALESCE(NULLIF(tenant_id, ''), ?)
    """, (PRIMARY_TENANT_ID,))

    conn.execute("""
    UPDATE rental_items
    SET tenant_id = COALESCE(NULLIF(tenant_id, ''), (
        SELECT tenant_id FROM rentals WHERE rentals.id = rental_items.rental_id
    ), ?)
    """, (PRIMARY_TENANT_ID,))

    conn.execute("""
    UPDATE outside_items
    SET tenant_id = COALESCE(NULLIF(tenant_id, ''), (
        SELECT tenant_id FROM rentals WHERE rentals.id = outside_items.rental_id
    ), ?)
    """, (PRIMARY_TENANT_ID,))

    conn.execute("""
    UPDATE clients
    SET tenant_id = COALESCE(NULLIF(tenant_id, ''), ?)
    """, (PRIMARY_TENANT_ID,))

    conn.commit()
    conn.close()


def seed_default_users():

    conn = get_db()

    defaults = [
        ("maheshbacham", "aA@9440984550", "owner", PRIMARY_TENANT_ID),
        ("ramesh", "rR@995956023", "manager", PRIMARY_TENANT_ID),
        (DEMO_USERNAME, DEMO_PASSWORD, "owner", DEMO_TENANT_ID)
    ]

    try:
        for username, raw_password, role, tenant_id in defaults:
            upsert_user(conn, username, raw_password, role, tenant_id)

        conn.commit()
    finally:
        conn.close()


def seed_demo_data():

    conn = get_db()
    try:
        if tenant_demo_data_exists(conn, DEMO_TENANT_ID):
            return

        demo_clients = [
            ("Sunrise Studios", "9000000001", "Banjara Hills, Hyderabad"),
            ("Frame & Focus", "9000000002", "Jubilee Hills, Hyderabad"),
        ]
        for name, phone, address in demo_clients:
            conn.execute("""
            INSERT INTO clients(tenant_id,name,phone,address)
            VALUES(?,?,?,?)
            """, (DEMO_TENANT_ID, name, phone, address))

        demo_items = [
            ("Sony FX3 Body", "Camera", 5500, "Rented"),
            ("Sony 24-70 GM II", "Lens", 2200, "Rented"),
            ("Aputure 300D", "Light", 1800, "Available"),
            ("DJI RS 3", "Gimbal", 1600, "Available"),
        ]
        item_ids = {}
        for name, category, rent_per_day, status in demo_items:
            if USE_POSTGRES:
                item_id = conn.execute("""
                INSERT INTO items(tenant_id,name,category,rent_per_day,status)
                VALUES(?,?,?,?,?)
                RETURNING id
                """, (DEMO_TENANT_ID, name, category, rent_per_day, status)).fetchone()["id"]
            else:
                conn.execute("""
                INSERT INTO items(tenant_id,name,category,rent_per_day,status)
                VALUES(?,?,?,?,?)
                """, (DEMO_TENANT_ID, name, category, rent_per_day, status))
                item_id = conn.execute(
                    "SELECT last_insert_rowid() AS id"
                ).fetchone()["id"]
            item_ids[name] = item_id

        if USE_POSTGRES:
            rental_id = conn.execute("""
            INSERT INTO rentals
            (tenant_id,customer_name,phone,start_date,end_date,total_amount,advance_paid,balance,status)
            VALUES(?,?,?,?,?,?,?,?,?)
            RETURNING id
            """, (
                DEMO_TENANT_ID,
                "Sunrise Studios",
                "9000000001",
                "2026-04-10",
                "2026-04-12",
                23100,
                15000,
                8100,
                "Active"
            )).fetchone()["id"]
        else:
            conn.execute("""
            INSERT INTO rentals
            (tenant_id,customer_name,phone,start_date,end_date,total_amount,advance_paid,balance,status)
            VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                DEMO_TENANT_ID,
                "Sunrise Studios",
                "9000000001",
                "2026-04-10",
                "2026-04-12",
                23100,
                15000,
                8100,
                "Active"
            ))
            rental_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        demo_rental_items = [
            (item_ids["Sony FX3 Body"], 5500, 3, 16500, "Active"),
            (item_ids["Sony 24-70 GM II"], 2200, 3, 6600, "Active"),
        ]
        for item_id, rate_per_day, days, total, status in demo_rental_items:
            conn.execute("""
            INSERT INTO rental_items
            (tenant_id,rental_id,item_id,rate_per_day,days,total,status)
            VALUES(?,?,?,?,?,?,?)
            """, (
                DEMO_TENANT_ID,
                rental_id,
                item_id,
                rate_per_day,
                days,
                total,
                status
            ))

        conn.execute("""
        INSERT INTO outside_items
        (tenant_id,rental_id,vendor_name,item_name,rate_per_day,days,total,paid,balance)
        VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            DEMO_TENANT_ID,
            rental_id,
            "Demo Lights Vendor",
            "Nanlite Forza 300",
            1200,
            3,
            3600,
            1000,
            2600
        ))

        conn.commit()
    finally:
        conn.close()


def remove_user(username):
    conn = get_db()
    try:
        conn.execute("DELETE FROM users WHERE LOWER(TRIM(username))=?", (username.strip().lower(),))
        conn.commit()
    finally:
        conn.close()


def ensure_db_ready():
    global DB_READY
    if DB_READY:
        return
    init_db()
    remove_user("gopi")
    seed_default_users()
    seed_demo_data()
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

        username = request.form["username"].strip().lower()
        password = request.form["password"]

        conn = get_db()

        user = get_user_by_username(conn, username)

        conn.close()

        if user and check_password_hash(user["password"], password):

            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["tenant_id"] = user["tenant_id"] or PRIMARY_TENANT_ID

            return redirect("/dashboard")

        return "Invalid login"

    return render_template(
        "login.html",
        demo_username=DEMO_USERNAME,
        demo_password=DEMO_PASSWORD
    )


@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        app.static_folder,
        "icon-192.png",
        mimetype="image/png"
    )


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return send_from_directory(
        app.static_folder,
        "apple-touch-icon.png",
        mimetype="image/png"
    )


@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon_precomposed():
    return send_from_directory(
        app.static_folder,
        "apple-touch-icon.png",
        mimetype="image/png"
    )


# -----------------------
# Dashboard
# -----------------------

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect("/")

    tenant_id_val = current_tenant_id()
    conn = get_db()
    active_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM rentals WHERE tenant_id=? AND status='Active'",
        (tenant_id_val,)
    ).fetchone()["cnt"]
    active_items = conn.execute(
        """SELECT i.name, ri.quantity
           FROM rental_items ri
           JOIN items i ON i.id = ri.item_id
           JOIN rentals r ON r.id = ri.rental_id
           WHERE r.tenant_id=? AND r.status='Active'
           ORDER BY i.name""",
        (tenant_id_val,)
    ).fetchall()
    conn.close()

    return render_template(
        "dashboard.html",
        username=session["username"],
        role=session["role"],
        tenant_id=tenant_id_val,
        demo_tenant_id=DEMO_TENANT_ID,
        active_count=active_count,
        active_items=active_items,
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

    tenant_id = current_tenant_id()
    conn = get_db()

    item_rows = conn.execute(
        "SELECT * FROM items WHERE tenant_id=? ORDER BY id DESC",
        (tenant_id,)
    ).fetchall()

    item_usage = {
        row["item_id"]: row["usage_count"]
        for row in conn.execute("""
        SELECT item_id, COUNT(*) AS usage_count
        FROM rental_items
        WHERE tenant_id=?
        GROUP BY item_id
        """, (tenant_id,)).fetchall()
    }

    clients = conn.execute(
        "SELECT * FROM clients WHERE tenant_id=? ORDER BY id DESC",
        (tenant_id,)
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
    custom_id = request.form.get("custom_id", "").strip() or None
    tenant_id = current_tenant_id()

    conn = get_db()

    conn.execute("""
    INSERT INTO items(tenant_id,name,category,rent_per_day,status,custom_id)
    VALUES(?,?,?,?,?,?)
    """, (tenant_id, name, category, rent, "Available", custom_id))

    conn.commit()
    conn.close()

    return redirect("/inventory")


@app.route("/edit_item/<int:item_id>", methods=["GET", "POST"])
def edit_item(item_id):

    if "user_id" not in session:
        return redirect("/")

    tenant_id = current_tenant_id()
    conn = get_db()

    item = conn.execute(
        "SELECT * FROM items WHERE id=? AND tenant_id=?",
        (item_id, tenant_id)
    ).fetchone()

    if not item:
        conn.close()
        return redirect("/inventory")

    if request.method == "POST":
        name = request.form["name"]
        category = request.form["category"]
        rent = request.form["rent"]
        custom_id = request.form.get("custom_id", "").strip() or None

        conn.execute("""
        UPDATE items
        SET name=?, category=?, rent_per_day=?, custom_id=?
        WHERE id=? AND tenant_id=?
        """, (name, category, rent, custom_id, item_id, tenant_id))

        conn.commit()
        conn.close()
        return redirect("/inventory")

    conn.close()
    return render_template("edit_item.html", item=item)


@app.route("/delete_item/<int:item_id>", methods=["POST"])
def delete_item(item_id):

    if "user_id" not in session:
        return redirect("/")

    tenant_id = current_tenant_id()
    conn = get_db()
    try:
        item = conn.execute(
            "SELECT status FROM items WHERE id=? AND tenant_id=?",
            (item_id, tenant_id)
        ).fetchone()
        if not item:
            return redirect("/inventory")

        item_usage = conn.execute(
            "SELECT COUNT(*) AS usage_count FROM rental_items WHERE item_id=? AND tenant_id=?",
            (item_id, tenant_id)
        ).fetchone()["usage_count"]

        if item["status"] == "Rented" or item_usage > 0:
            return redirect("/inventory")

        conn.execute(
            "DELETE FROM items WHERE id=? AND tenant_id=?",
            (item_id, tenant_id)
        )
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
    tenant_id = current_tenant_id()

    conn = get_db()

    conn.execute("""
    INSERT INTO clients(tenant_id,name,phone,address)
    VALUES(?,?,?,?)
    """, (tenant_id, name, phone, address))

    conn.commit()
    conn.close()

    return redirect("/inventory")


@app.route("/edit_client/<int:client_id>", methods=["GET", "POST"])
def edit_client(client_id):

    if "user_id" not in session:
        return redirect("/")

    tenant_id = current_tenant_id()
    conn = get_db()

    client = conn.execute(
        "SELECT * FROM clients WHERE id=? AND tenant_id=?",
        (client_id, tenant_id)
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
        WHERE id=? AND tenant_id=?
        """, (name, phone, address, client_id, tenant_id))

        conn.commit()
        conn.close()
        return redirect("/inventory")

    conn.close()
    return render_template("edit_client.html", client=client)


@app.route("/delete_client/<int:client_id>", methods=["POST"])
def delete_client(client_id):

    if "user_id" not in session:
        return redirect("/")

    tenant_id = current_tenant_id()
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM clients WHERE id=? AND tenant_id=?",
            (client_id, tenant_id)
        )
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

    tenant_id = current_tenant_id()
    conn = get_db()

    items = conn.execute(
        "SELECT * FROM items WHERE tenant_id=?",
        (tenant_id,)
    ).fetchall()

    clients = conn.execute(
        "SELECT * FROM clients WHERE tenant_id=? ORDER BY name ASC, id DESC",
        (tenant_id,)
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
    tenant_id = current_tenant_id()

    conn = get_db()

    if USE_POSTGRES:
        rental_id = conn.execute("""
        INSERT INTO rentals
        (tenant_id,customer_name,phone,start_date,end_date,
        total_amount,advance_paid,balance,status)
        VALUES(?,?,?,?,?,?,?,?,?)
        RETURNING id
        """, (
            tenant_id,
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
        (tenant_id,customer_name,phone,start_date,end_date,
        total_amount,advance_paid,balance,status)
        VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            tenant_id,
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
            "SELECT * FROM items WHERE id=? AND tenant_id=?",
            (item_id, tenant_id)
        ).fetchone()
        if not item:
            continue

        try:
            quantity = max(1, int(request.form.get(f"item_quantities[{item_id}]", 1) or 1))
        except (ValueError, TypeError):
            quantity = 1

        item_total = days * item["rent_per_day"] * quantity

        total_amount += item_total

        conn.execute("""
        INSERT INTO rental_items
        (tenant_id,rental_id,item_id,rate_per_day,days,total,status,quantity)
        VALUES(?,?,?,?,?,?,?,?)
        """, (
            tenant_id,
            rental_id,
            item_id,
            item["rent_per_day"],
            days,
            item_total,
            "Active",
            quantity
        ))

        conn.execute(
            "UPDATE items SET status='Rented' WHERE id=? AND tenant_id=?",
            (item_id, tenant_id)
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
            "SELECT name, rent_per_day FROM items WHERE id=? AND tenant_id=?",
            (vendor_item_id, tenant_id)
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

        try:
            vendor_qty = max(1, int(request.form.get(f"vendor_quantities[{vendor_item_id}]", 1) or 1))
        except (ValueError, TypeError):
            vendor_qty = 1

        vendor_total = days * rate_per_day * vendor_qty

        conn.execute("""
        INSERT INTO outside_items
        (tenant_id,rental_id,vendor_name,item_name,rate_per_day,days,total,paid,balance,quantity)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            tenant_id,
            rental_id,
            vendor_name,
            item["name"],
            rate_per_day,
            days,
            vendor_total,
            0,
            vendor_total,
            vendor_qty
        ))

    balance = total_amount - advance_paid

    conn.execute("""
    UPDATE rentals
    SET total_amount=?, balance=?
    WHERE id=? AND tenant_id=?
    """, (total_amount, balance, rental_id, tenant_id))

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

    tenant_id = current_tenant_id()
    conn = get_db()

    rental_rows = conn.execute(
        "SELECT * FROM rentals WHERE tenant_id=? ORDER BY start_date DESC, id DESC",
        (tenant_id,)
    ).fetchall()

    rental_item_rows = conn.execute("""
    SELECT ri.rental_id, i.name, ri.rate_per_day
    FROM rental_items ri
    JOIN items i ON i.id = ri.item_id
    WHERE ri.tenant_id=?
    ORDER BY ri.rental_id DESC, i.name ASC
    """, (tenant_id,)).fetchall()

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
        given_date_display = ""

        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            rental_days = (end_dt - start_dt).days + 1
            if rental_days < 0:
                rental_days = 0
            given_date_display = start_dt.strftime("%d/%m/%y")
        except (TypeError, ValueError):
            given_date_display = start_date or ""

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

    tenant_id = current_tenant_id()
    conn = get_db()
    try:
        rental = conn.execute(
            "SELECT status FROM rentals WHERE id=? AND tenant_id=?",
            (rental_id, tenant_id)
        ).fetchone()

        if not rental:
            return redirect("/rental_records")

        # One-way transition: once returned, keep it returned.
        if rental["status"] != "Active":
            return redirect("/rental_records")

        new_status = "Returned"

        conn.execute(
            "UPDATE rentals SET status=? WHERE id=? AND tenant_id=?",
            (new_status, rental_id, tenant_id)
        )

        item_ids = conn.execute(
            "SELECT item_id FROM rental_items WHERE rental_id=? AND tenant_id=?",
            (rental_id, tenant_id)
        ).fetchall()

        for row in item_ids:
            conn.execute(
                "UPDATE items SET status=? WHERE id=? AND tenant_id=?",
                (
                    "Available",
                    row["item_id"],
                    tenant_id
                )
            )

        conn.execute(
            "UPDATE rental_items SET status=? WHERE rental_id=? AND tenant_id=?",
            (new_status, rental_id, tenant_id)
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

    tenant_id = current_tenant_id()
    conn = get_db()

    rental = conn.execute(
        "SELECT * FROM rentals WHERE id=? AND tenant_id=?",
        (rental_id, tenant_id)
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
                "SELECT item_id FROM rental_items WHERE rental_id=? AND tenant_id=?",
                (rental_id, tenant_id)
            ).fetchall()
            previous_item_ids = [row["item_id"] for row in previous_item_rows]
            previous_outside_rows = conn.execute("""
            SELECT item_name, paid
            FROM outside_items
            WHERE rental_id=? AND tenant_id=?
            """, (rental_id, tenant_id)).fetchall()
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
                    f"SELECT * FROM items WHERE tenant_id=? AND id IN ({placeholders})",
                    (tenant_id, *selected_item_ids)
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
                    f"SELECT id, name, rent_per_day FROM items WHERE tenant_id=? AND id IN ({vendor_placeholders})",
                    (tenant_id, *vendor_item_ids)
                ).fetchall()
                vendor_item_lookup = {row["id"]: row for row in vendor_items}

            conn.execute("""
            UPDATE rentals
            SET customer_name=?, phone=?, start_date=?, end_date=?, advance_paid=?, status=?
            WHERE id=? AND tenant_id=?
            """, (
                customer_name,
                phone,
                start_date,
                end_date,
                advance_paid,
                status,
                rental_id,
                tenant_id
            ))

            for item_id in previous_item_ids:
                conn.execute(
                    "UPDATE items SET status='Available' WHERE id=? AND tenant_id=?",
                    (item_id, tenant_id)
                )

            conn.execute(
                "DELETE FROM rental_items WHERE rental_id=? AND tenant_id=?",
                (rental_id, tenant_id)
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

                try:
                    quantity = max(1, int(request.form.get(f"item_quantities[{item_id}]", 1) or 1))
                except (ValueError, TypeError):
                    quantity = 1

                item_total = rate_per_day * days * quantity
                total_amount += item_total

                conn.execute("""
                INSERT INTO rental_items
                (tenant_id,rental_id,item_id,rate_per_day,days,total,status,quantity)
                VALUES(?,?,?,?,?,?,?,?)
                """, (
                    tenant_id,
                    rental_id,
                    item_id,
                    rate_per_day,
                    days,
                    item_total,
                    status,
                    quantity
                ))

                conn.execute(
                    "UPDATE items SET status=? WHERE id=? AND tenant_id=?",
                    ("Rented" if status == "Active" else "Available", item_id, tenant_id)
                )

            conn.execute(
                "DELETE FROM outside_items WHERE rental_id=? AND tenant_id=?",
                (rental_id, tenant_id)
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

                try:
                    vendor_qty = max(1, int(request.form.get(f"vendor_quantities[{vendor_item_id}]", 1) or 1))
                except (ValueError, TypeError):
                    vendor_qty = 1

                vendor_total = rate_per_day * days * vendor_qty
                paid = min(previous_outside_payments.get(item["name"], 0), vendor_total)
                balance_due = vendor_total - paid

                conn.execute("""
                INSERT INTO outside_items
                (tenant_id,rental_id,vendor_name,item_name,rate_per_day,days,total,paid,balance,quantity)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (
                    tenant_id,
                    rental_id,
                    vendor_name,
                    item["name"],
                    rate_per_day,
                    days,
                    vendor_total,
                    paid,
                    balance_due,
                    vendor_qty
                ))

            balance = total_amount - advance_paid

            conn.execute("""
            UPDATE rentals
            SET total_amount=?, balance=?
            WHERE id=? AND tenant_id=?
            """, (total_amount, balance, rental_id, tenant_id))

            conn.commit()
        finally:
            conn.close()

        return redirect("/rental_records")

    clients = conn.execute(
        "SELECT * FROM clients WHERE tenant_id=? ORDER BY name ASC, id DESC",
        (tenant_id,)
    ).fetchall()

    current_rental_items = conn.execute("""
    SELECT item_id, rate_per_day, quantity
    FROM rental_items
    WHERE rental_id=? AND tenant_id=?
    """, (rental_id, tenant_id)).fetchall()
    current_outside_items = conn.execute("""
    SELECT vendor_name, item_name, rate_per_day, quantity
    FROM outside_items
    WHERE rental_id=? AND tenant_id=?
    ORDER BY id ASC
    """, (rental_id, tenant_id)).fetchall()

    selected_item_ids = [row["item_id"] for row in current_rental_items]
    selected_rates = {
        row["item_id"]: row["rate_per_day"] for row in current_rental_items
    }
    selected_quantities = {
        row["item_id"]: (row["quantity"] or 1) for row in current_rental_items
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
    selected_vendor_quantities = {
        row["item_name"]: (row["quantity"] or 1) for row in current_outside_items
    }

    if selected_item_ids:
        placeholders = ",".join("?" for _ in selected_item_ids)
        items = conn.execute(
            f"""
            SELECT *
            FROM items
            WHERE tenant_id=? AND (status='Available' OR id IN ({placeholders}))
            ORDER BY name ASC, id DESC
            """,
            (tenant_id, *selected_item_ids)
        ).fetchall()
    else:
        items = conn.execute("""
        SELECT *
        FROM items
        WHERE tenant_id=? AND status='Available'
        ORDER BY name ASC, id DESC
        """, (tenant_id,)).fetchall()

    vendor_items = conn.execute("""
    SELECT id, name, rent_per_day
    FROM items
    WHERE tenant_id=?
    ORDER BY name ASC, id DESC
    """, (tenant_id,)).fetchall()

    conn.close()
    return render_template(
        "edit_rental.html",
        rental=rental,
        clients=clients,
        items=items,
        vendor_items=vendor_items,
        selected_item_ids=selected_item_ids,
        selected_rates=selected_rates,
        selected_quantities=selected_quantities,
        outside_vendor_name=outside_vendor_name,
        selected_vendor_item_names=selected_vendor_item_names,
        selected_vendor_rates=selected_vendor_rates,
        selected_vendor_quantities=selected_vendor_quantities
    )


@app.route("/delete_rental/<int:rental_id>", methods=["POST"])
def delete_rental(rental_id):

    if "user_id" not in session:
        return redirect("/")

    if session.get("role") != "owner":
        return redirect("/rental_records")

    tenant_id = current_tenant_id()
    conn = get_db()
    try:
        item_ids = conn.execute(
            "SELECT item_id FROM rental_items WHERE rental_id=? AND tenant_id=?",
            (rental_id, tenant_id)
        ).fetchall()

        for row in item_ids:
            conn.execute(
                "UPDATE items SET status='Available' WHERE id=? AND tenant_id=?",
                (row["item_id"], tenant_id)
            )

        conn.execute(
            "DELETE FROM rental_items WHERE rental_id=? AND tenant_id=?",
            (rental_id, tenant_id)
        )
        conn.execute(
            "DELETE FROM outside_items WHERE rental_id=? AND tenant_id=?",
            (rental_id, tenant_id)
        )
        conn.execute(
            "DELETE FROM rentals WHERE id=? AND tenant_id=?",
            (rental_id, tenant_id)
        )
        conn.commit()
    finally:
        conn.close()

    return redirect("/rental_records")


# -----------------------
# Credit report
# -----------------------

@app.route("/generate_invoice")
def generate_invoice_page():
    if "user_id" not in session:
        return redirect("/")
    tenant_id = current_tenant_id()
    conn = get_db()
    rentals = conn.execute(
        "SELECT * FROM rentals WHERE tenant_id=? ORDER BY start_date DESC, id DESC",
        (tenant_id,)
    ).fetchall()
    conn.close()
    return render_template("generate_invoice.html", rentals=rentals)


@app.route("/vendor_dues")
def vendor_dues_page():
    if "user_id" not in session:
        return redirect("/")
    tenant_id = current_tenant_id()
    conn = get_db()
    vendor_rows = conn.execute(
        """SELECT oi.*, r.start_date
           FROM outside_items oi
           JOIN rentals r ON r.id = oi.rental_id
           WHERE oi.tenant_id=?
           ORDER BY r.start_date DESC, oi.id DESC""",
        (tenant_id,)
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
                "total": 0, "paid": 0, "balance": 0, "items": []
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
    return render_template("vendor_dues.html", vendors=grouped_vendors)


@app.route("/customer_dues")
def customer_dues_page():
    if "user_id" not in session:
        return redirect("/")
    tenant_id = current_tenant_id()
    conn = get_db()
    rentals = conn.execute(
        "SELECT * FROM rentals WHERE tenant_id=? ORDER BY start_date DESC, id DESC",
        (tenant_id,)
    ).fetchall()
    conn.close()
    return render_template("customer_dues.html", rentals=rentals)


@app.route("/credit_report")
def credit_report():

    if "user_id" not in session:
        return redirect("/")

    tenant_id = current_tenant_id()
    conn = get_db()

    rentals = conn.execute(
        "SELECT * FROM rentals WHERE tenant_id=? ORDER BY start_date DESC, id DESC",
        (tenant_id,)
    ).fetchall()

    vendor_rows = conn.execute(
        """
        SELECT oi.*, r.start_date
        FROM outside_items oi
        JOIN rentals r ON r.id = oi.rental_id
        WHERE oi.tenant_id=?
        ORDER BY r.start_date DESC, oi.id DESC
        """,
        (tenant_id,)
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

    return render_template(
        "credit_report.html",
        rentals=rentals,
        vendors=grouped_vendors,
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

    tenant_id = current_tenant_id()
    conn = get_db()
    try:
        rental = conn.execute(
            "SELECT total_amount, advance_paid FROM rentals WHERE id=? AND tenant_id=?",
            (rental_id, tenant_id)
        ).fetchone()

        if not rental:
            return redirect("/credit_report")

        updated_paid = min(rental["total_amount"], rental["advance_paid"] + payment)
        balance = rental["total_amount"] - updated_paid

        conn.execute(
            "UPDATE rentals SET advance_paid=?, balance=? WHERE id=? AND tenant_id=?",
            (updated_paid, balance, rental_id, tenant_id)
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

    tenant_id = current_tenant_id()
    conn = get_db()
    try:
        vendor = conn.execute(
            "SELECT total, paid FROM outside_items WHERE id=? AND tenant_id=?",
            (vendor_id, tenant_id)
        ).fetchone()

        if not vendor:
            return redirect("/credit_report")

        updated_paid = min(vendor["total"], (vendor["paid"] or 0) + payment)
        balance = vendor["total"] - updated_paid

        conn.execute(
            "UPDATE outside_items SET paid=?, balance=? WHERE id=? AND tenant_id=?",
            (updated_paid, balance, vendor_id, tenant_id)
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

    tenant_id = current_tenant_id()
    conn = get_db()
    try:
        vendor_rows = conn.execute("""
        SELECT id,total,COALESCE(paid,0) AS paid,COALESCE(balance,total-COALESCE(paid,0)) AS balance
        FROM outside_items
        WHERE rental_id=? AND tenant_id=? AND COALESCE(vendor_name,'')=?
        ORDER BY id ASC
        """, (rental_id, tenant_id, vendor_name)).fetchall()

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
                "UPDATE outside_items SET paid=?, balance=? WHERE id=? AND tenant_id=?",
                (updated_paid, updated_balance, row["id"], tenant_id)
            )
            payment_left -= row_payment

        conn.commit()
    finally:
        conn.close()

    return redirect("/credit_report")


def num_to_words(n):
    n = int(round(n))
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
            "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def _below_1000(x):
        if x == 0:
            return ""
        elif x < 20:
            return ones[x]
        elif x < 100:
            rest = " " + ones[x % 10] if x % 10 else ""
            return tens[x // 10] + rest
        else:
            rest = " " + _below_1000(x % 100) if x % 100 else ""
            return ones[x // 100] + " Hundred" + rest

    if n == 0:
        return "Zero Rupees Only"
    parts = []
    if n >= 10000000:
        parts.append(_below_1000(n // 10000000) + " Crore")
        n %= 10000000
    if n >= 100000:
        parts.append(_below_1000(n // 100000) + " Lakh")
        n %= 100000
    if n >= 1000:
        parts.append(_below_1000(n // 1000) + " Thousand")
        n %= 1000
    if n > 0:
        parts.append(_below_1000(n))
    return " ".join(parts) + " Rupees Only"


@app.route("/invoice/new/<int:rental_id>")
def invoice_new(rental_id):
    if "user_id" not in session:
        return redirect("/")
    tenant_id = current_tenant_id()
    conn = get_db()
    rental = conn.execute(
        "SELECT * FROM rentals WHERE id=? AND tenant_id=?",
        (rental_id, tenant_id)
    ).fetchone()
    conn.close()
    if not rental:
        return redirect("/credit_report")
    back_url = request.args.get("from", f"/payment/{rental_id}")
    return render_template("invoice_form.html", rental=rental, back_url=back_url)


@app.route("/invoice/create/<int:rental_id>", methods=["POST"])
def invoice_create(rental_id):
    if "user_id" not in session:
        return redirect("/")
    tenant_id = current_tenant_id()
    gst_type = request.form.get("gst_type", "no_gst")
    customer_gstin = request.form.get("customer_gstin", "").strip().upper()
    customer_gst_name = request.form.get("customer_gst_name", "").strip()
    today = datetime.now()
    today_str = today.strftime("%Y%m%d")
    invoice_date = today.strftime("%Y-%m-%d")
    created_at = today.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM invoices WHERE tenant_id=? AND invoice_number LIKE ?",
            (tenant_id, f"INV-{today_str}%")
        ).fetchone()
        count = row["cnt"]
        invoice_number = f"INV-{today_str}{count + 1:02d}"
        if USE_POSTGRES:
            invoice_id = conn.execute(
                """INSERT INTO invoices(tenant_id,rental_id,invoice_number,invoice_date,gst_type,customer_gstin,customer_gst_name,created_at)
                   VALUES(?,?,?,?,?,?,?,?) RETURNING id""",
                (tenant_id, rental_id, invoice_number, invoice_date, gst_type, customer_gstin, customer_gst_name, created_at)
            ).fetchone()["id"]
        else:
            conn.execute(
                """INSERT INTO invoices(tenant_id,rental_id,invoice_number,invoice_date,gst_type,customer_gstin,customer_gst_name,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (tenant_id, rental_id, invoice_number, invoice_date, gst_type, customer_gstin, customer_gst_name, created_at)
            )
            invoice_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect(f"/invoice/{invoice_id}")


@app.route("/invoice/<int:invoice_id>")
def invoice_view(invoice_id):
    if "user_id" not in session:
        return redirect("/")
    tenant_id = current_tenant_id()
    conn = get_db()
    invoice = conn.execute(
        "SELECT * FROM invoices WHERE id=? AND tenant_id=?",
        (invoice_id, tenant_id)
    ).fetchone()
    if not invoice:
        conn.close()
        return redirect("/credit_report")
    rental = conn.execute(
        "SELECT * FROM rentals WHERE id=? AND tenant_id=?",
        (invoice["rental_id"], tenant_id)
    ).fetchone()
    items = conn.execute(
        """SELECT i.name, ri.rate_per_day, ri.days, ri.total,
                  COALESCE(ri.quantity, 1) AS quantity
           FROM rental_items ri
           JOIN items i ON i.id = ri.item_id
           WHERE ri.rental_id=? AND ri.tenant_id=?""",
        (invoice["rental_id"], tenant_id)
    ).fetchall()
    conn.close()
    sub_total = sum(row["total"] or 0 for row in items)
    cgst = round(sub_total * 0.09, 2) if invoice["gst_type"] == "gst" else 0
    sgst = round(sub_total * 0.09, 2) if invoice["gst_type"] == "gst" else 0
    grand_total = sub_total + cgst + sgst
    return render_template(
        "invoice.html",
        invoice=invoice,
        rental=rental,
        items=items,
        sub_total=sub_total,
        cgst=cgst,
        sgst=sgst,
        grand_total=grand_total,
        amount_in_words=num_to_words(grand_total),
    )


@app.route("/payment/<int:rental_id>", methods=["GET", "POST"])
def payment_page(rental_id):

    if "user_id" not in session:
        return redirect("/")

    tenant_id = current_tenant_id()
    conn = get_db()

    rental = conn.execute(
        "SELECT * FROM rentals WHERE id=? AND tenant_id=?",
        (rental_id, tenant_id)
    ).fetchone()

    if not rental:
        conn.close()
        return redirect("/credit_report")

    if request.method == "POST":
        payment_text = request.form.get("payment", "").strip()
        try:
            payment = float(payment_text)
        except ValueError:
            payment = 0
        if payment > 0:
            updated_paid = min(rental["total_amount"], rental["advance_paid"] + payment)
            balance = rental["total_amount"] - updated_paid
            conn.execute(
                "UPDATE rentals SET advance_paid=?, balance=? WHERE id=? AND tenant_id=?",
                (updated_paid, balance, rental_id, tenant_id)
            )
            conn.commit()
        conn.close()
        return redirect(f"/payment/{rental_id}")

    items = conn.execute(
        """SELECT i.name, ri.rate_per_day, ri.days, ri.total
           FROM rental_items ri
           JOIN items i ON i.id = ri.item_id
           WHERE ri.rental_id=? AND ri.tenant_id=?""",
        (rental_id, tenant_id)
    ).fetchall()
    conn.close()

    return render_template("payment.html", rental=rental, items=items)


@app.route("/vendor_payment/<int:rental_id>", methods=["GET", "POST"])
def vendor_payment_page(rental_id):

    if "user_id" not in session:
        return redirect("/")

    vendor_name = request.args.get("vendor", "").strip()
    tenant_id = current_tenant_id()
    conn = get_db()

    rental = conn.execute(
        "SELECT * FROM rentals WHERE id=? AND tenant_id=?",
        (rental_id, tenant_id)
    ).fetchone()

    if not rental:
        conn.close()
        return redirect("/credit_report")

    if request.method == "POST":
        vendor_name = request.form.get("vendor_name", vendor_name).strip()
        payment_text = request.form.get("payment", "").strip()
        try:
            payment = float(payment_text)
        except ValueError:
            payment = 0
        if payment > 0:
            vendor_rows = conn.execute(
                """SELECT id,total,COALESCE(paid,0) AS paid,
                   COALESCE(balance,total-COALESCE(paid,0)) AS balance
                   FROM outside_items
                   WHERE rental_id=? AND tenant_id=? AND COALESCE(vendor_name,'')=?
                   ORDER BY id ASC""",
                (rental_id, tenant_id, vendor_name)
            ).fetchall()
            remaining = min(payment, sum(r["balance"] for r in vendor_rows))
            for row in vendor_rows:
                if remaining <= 0:
                    break
                row_pay = min(row["balance"], remaining)
                if row_pay > 0:
                    conn.execute(
                        "UPDATE outside_items SET paid=?, balance=? WHERE id=? AND tenant_id=?",
                        (row["paid"] + row_pay, row["total"] - row["paid"] - row_pay, row["id"], tenant_id)
                    )
                    remaining -= row_pay
            conn.commit()
        conn.close()
        return redirect(f"/vendor_payment/{rental_id}?vendor={quote_plus(vendor_name)}")

    vendor_items = conn.execute(
        """SELECT * FROM outside_items
           WHERE rental_id=? AND tenant_id=? AND COALESCE(vendor_name,'')=?""",
        (rental_id, tenant_id, vendor_name)
    ).fetchall()
    conn.close()

    total = sum(r["total"] or 0 for r in vendor_items)
    paid = sum(r["paid"] or 0 for r in vendor_items)
    balance = total - paid

    return render_template("vendor_payment.html",
                           rental=rental,
                           vendor_name=vendor_name,
                           items=vendor_items,
                           total=total, paid=paid, balance=balance)


# -----------------------
# Run app (local only)
# -----------------------

if __name__ == "__main__":
    app.run(debug=True)
