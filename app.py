"""AgriRent: a small, server-rendered tractor rental application."""

import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask, abort, current_app, flash, g, jsonify, redirect, render_template,
    request, session, url_for
)
from flask_cors import CORS
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from database import close_db, get_db, init_db


app = Flask(__name__, instance_relative_config=True)
app.config.update(
    SECRET_KEY=os.environ.get("AGRIRENT_SECRET_KEY") or secrets.token_hex(32),
    DATABASE=os.environ.get(
        "AGRIRENT_DATABASE", str(Path(app.instance_path) / "agrirent.sqlite")
    ),
    ADVANCE_PERCENT=25,
    API_TOKEN_MAX_AGE=86400,
    CORS_ORIGINS=os.environ.get("AGRIRENT_CORS_ORIGINS", "*"),
)
cors_origins = (
    ["*"] if app.config["CORS_ORIGINS"] == "*"
    else [origin.strip() for origin in app.config["CORS_ORIGINS"].split(",") if origin.strip()]
)
CORS(
    app,
    resources={r"/api/*": {"origins": cors_origins}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    supports_credentials=False,
)
Path(app.instance_path).mkdir(parents=True, exist_ok=True)
app.teardown_appcontext(close_db)

with app.app_context():
    init_db()


def csrf_token():
    """Return a session-bound token included in every form that changes data."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token
quote_signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="agrirent-booking-quote")


@app.before_request
def check_csrf():
    if request.path.startswith("/api/"):
        return
    if request.method == "POST":
        submitted = request.form.get("_csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not submitted or not expected or not secrets.compare_digest(submitted, expected):
            abort(400, description="The form security token is missing or expired. Please go back and try again.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(role):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if session.get("role") != role:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute(
        "SELECT id, customer_code, name, email, phone, role FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()


@app.context_processor
def inject_user():
    return {
        "current_user": current_user(),
        "today": date.today().isoformat(),
        "advance_percent": app.config["ADVANCE_PERCENT"],
    }


def parse_money(value, field_name, allow_zero=True):
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{field_name} must be a valid number.")
    if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0):
        raise ValueError(f"{field_name} must be {'greater than zero' if not allow_zero else 'zero or greater'}.")
    if amount > Decimal("100000000"):
        raise ValueError(f"{field_name} must not exceed 100,000,000 rupees.")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def cents(value):
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_rupees(amount_cents):
    return f"₹{amount_cents / 100:,.2f}"


app.jinja_env.filters["rupees"] = format_rupees


def owned_listing(listing_id, owner_id=None, active_only=False):
    db = get_db()
    query = """
        SELECT l.*, u.name AS owner_name, u.email AS owner_email
        FROM tractor_listings l JOIN users u ON u.id = l.owner_id
        WHERE l.id = ?
    """
    params = [listing_id]
    if owner_id is not None:
        query += " AND l.owner_id = ?"
        params.append(owner_id)
    if active_only:
        query += " AND l.active = 1"
    return db.execute(query, params).fetchone()


def booking_for_customer(booking_id, customer_id):
    return get_db().execute(
        """SELECT b.*, l.name AS tractor_name, l.brand, l.model, l.location,
                  l.phone AS owner_phone, l.owner_id, u.name AS customer_name,
                  u.phone AS customer_phone, owner.name AS owner_name
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = b.customer_id
           JOIN users owner ON owner.id = l.owner_id
           WHERE b.id = ? AND b.customer_id = ?""",
        (booking_id, customer_id),
    ).fetchone()


def booking_for_owner(booking_id, owner_id):
    return get_db().execute(
        """SELECT b.*, l.name AS tractor_name, l.brand, l.model, l.location,
                  l.owner_id, u.name AS customer_name, u.phone AS customer_phone,
                  u.email AS customer_email, u.customer_code
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = b.customer_id
           WHERE b.id = ? AND l.owner_id = ?""",
        (booking_id, owner_id),
    ).fetchone()


def customer_classification(owner_id, customer_id, exclude_booking_id=None):
    history = get_db().execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN b.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                  MAX(b.work_date) AS most_recent
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           WHERE l.owner_id = ? AND b.customer_id = ?
             AND (? IS NULL OR b.id != ?)""",
        (owner_id, customer_id, exclude_booking_id, exclude_booking_id),
    ).fetchone()
    if history["total"] == 0:
        return "New Customer"
    if history["most_recent"] and history["most_recent"] < (date.today() - timedelta(days=90)).isoformat():
        return "Inactive Customer"
    if history["completed"] and history["completed"] >= 3:
        return "Regular Customer"
    return "Repeat Customer"


def verified_paid_cents(booking_id):
    row = get_db().execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS paid FROM payments WHERE booking_id = ? AND status = 'verified'",
        (booking_id,),
    ).fetchone()
    return row["paid"]


def booking_required_cents(booking):
    if booking["payment_plan"] == "full_advance":
        return booking["total_cents"]
    if booking["payment_plan"] == "advance":
        return booking["advance_cents"]
    return 0


def payment_due_cents(booking, paid_cents):
    if booking["status"] == "completed":
        return max(booking["total_cents"] - paid_cents, 0)
    if booking["status"] != "awaiting_payment":
        return 0
    if booking["payment_plan"] == "full_advance":
        return max(booking["total_cents"] - paid_cents, 0)
    if booking["payment_plan"] == "advance":
        return max(booking["advance_cents"] - paid_cents, 0)
    return 0


def read_equipment_text(text):
    """Parse one optional equipment entry per line: equipment name | charge."""
    if len(text) > 8000:
        raise ValueError("Equipment details are too long.")
    entries = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit("|", 1)
        if len(parts) != 2 or not parts[0].strip():
            raise ValueError("Enter each equipment item as: name | charge")
        name = parts[0].strip()
        if len(name) > 80 or name.casefold() in seen:
            raise ValueError("Equipment names must be unique and no longer than 80 characters.")
        if len(entries) >= 50:
            raise ValueError("A listing can contain at most 50 equipment items.")
        seen.add(name.casefold())
        charge = parse_money(parts[1], f"Charge for {name}")
        entries.append((name, float(charge)))
    return entries


@app.route("/")
def index():
    listings = get_db().execute(
        """SELECT l.*, u.name AS owner_name FROM tractor_listings l
           JOIN users u ON u.id = l.owner_id WHERE l.active = 1
           ORDER BY l.created_at DESC LIMIT 6"""
    ).fetchall()
    return render_template("index.html", listings=listings)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        if (not name or len(name) > 100 or email.count("@") != 1 or
                any(character.isspace() for character in email) or
                email.startswith("@") or email.endswith("@") or len(email) > 254):
            flash("Enter a name and valid email address.", "error")
        elif not phone or len(phone) > 30:
            flash("Enter a valid contact phone number.", "error")
        elif len(password) < 8 or len(password) > 128:
            flash("Password must be between 8 and 128 characters.", "error")
        elif role not in ("owner", "customer"):
            flash("Choose Owner or Customer.", "error")
        else:
            db = get_db()
            try:
                code = "AR-" + secrets.token_hex(5).upper() if role == "customer" else None
                db.execute(
                    "INSERT INTO users (customer_code, name, email, phone, password_hash, role) VALUES (?, ?, ?, ?, ?, ?)",
                    (code, name, email, phone, generate_password_hash(password), role),
                )
                db.commit()
                flash("Registration successful. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError as error:
                db.rollback()
                if "UNIQUE constraint failed: users.email" in str(error):
                    flash("That email address is already registered.", "error")
                else:
                    raise
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (request.form.get("email", "").strip(),),
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Email or password was not recognized.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            csrf_token()
            target = request.args.get("next", "")
            parsed_target = urlsplit(target)
            if (target.startswith("/") and not target.startswith("//") and
                    "\\" not in target and not parsed_target.scheme and not parsed_target.netloc):
                return redirect(target)
            return redirect(url_for("owner_dashboard" if user["role"] == "owner" else "customer_dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/tractors")
def tractors():
    search = request.args.get("q", "").strip()
    location_filter = request.args.get("location", "").strip()
    tractor_type = request.args.get("type", "").strip()
    date_filter = request.args.get("work_date", "").strip()
    db = get_db()
    query = """SELECT l.*, u.name AS owner_name FROM tractor_listings l
               JOIN users u ON u.id = l.owner_id WHERE l.active = 1"""
    params = []
    if search:
        query += """ AND (l.name LIKE ? OR l.brand LIKE ? OR l.model LIKE ?
                          OR l.location LIKE ? OR EXISTS (
                              SELECT 1 FROM equipment e
                              WHERE e.listing_id = l.id AND e.name LIKE ?
                          ))"""
        params.extend([f"%{search}%"] * 5)
    if location_filter:
        query += " AND l.location LIKE ?"
        params.append(f"%{location_filter}%")
    if tractor_type:
        query += """ AND (l.name LIKE ? OR l.brand LIKE ? OR l.model LIKE ?
                          OR EXISTS (SELECT 1 FROM equipment e
                                     WHERE e.listing_id = l.id AND e.name LIKE ?))"""
        params.extend([f"%{tractor_type}%"] * 4)
    if date_filter:
        try:
            date.fromisoformat(date_filter)
        except ValueError:
            date_filter = ""
        if date_filter:
            query += """ AND EXISTS (SELECT 1 FROM availability a
                                     WHERE a.listing_id = l.id AND a.available_date = ?)"""
            params.append(date_filter)
    query += " ORDER BY l.created_at DESC"
    listings = db.execute(query, params).fetchall()
    return render_template(
        "tractors.html", listings=listings, search=search,
        location_filter=location_filter, tractor_type=tractor_type,
        date_filter=date_filter,
    )


@app.route("/tractors/<int:listing_id>")
def tractor_details(listing_id):
    listing = owned_listing(listing_id, active_only=True)
    if listing is None:
        abort(404)
    db = get_db()
    equipment_rows = db.execute(
        "SELECT * FROM equipment WHERE listing_id = ? ORDER BY name", (listing_id,)
    ).fetchall()
    availability_rows = db.execute(
        """SELECT * FROM availability WHERE listing_id = ? AND available_date >= ?
           ORDER BY available_date, start_time LIMIT 30""",
        (listing_id, date.today().isoformat()),
    ).fetchall()
    return render_template(
        "tractor_details.html", listing=listing, equipment=equipment_rows,
        availability=availability_rows,
    )


def listing_form_values():
    required = ("name", "brand", "model", "location", "phone")
    values = {key: request.form.get(key, "").strip() for key in required}
    values["photo_url"] = request.form.get("photo_url", "").strip()
    values["services"] = request.form.get("services", "").strip()
    if any(not values[key] for key in required):
        raise ValueError("Name, brand, model, location and phone are required.")
    if any(len(values[key]) > 200 for key in required) or len(values["services"]) > 2000:
        raise ValueError("Some listing details are too long.")
    if len(values["photo_url"]) > 2000:
        raise ValueError("Photo URL is too long.")
    if values["photo_url"] and not values["photo_url"].startswith(("https://", "http://")):
        raise ValueError("Photo URL must start with http:// or https://.")
    values["hourly_rate"] = float(parse_money(request.form.get("hourly_rate", ""), "Hourly rate"))
    values["acre_rate"] = float(parse_money(request.form.get("acre_rate", ""), "Per-acre rate"))
    values["equipment"] = read_equipment_text(request.form.get("equipment", ""))
    if values["hourly_rate"] == 0 and values["acre_rate"] == 0:
        raise ValueError("Set at least one of the hourly rate or per-acre rate.")
    return values


def save_equipment(db, listing_id, equipment):
    db.execute(
        """UPDATE booking_equipment SET equipment_id = NULL
           WHERE equipment_id IN (SELECT id FROM equipment WHERE listing_id = ?)""",
        (listing_id,),
    )
    db.execute("DELETE FROM equipment WHERE listing_id = ?", (listing_id,))
    db.executemany(
        "INSERT INTO equipment (listing_id, name, charge) VALUES (?, ?, ?)",
        [(listing_id, name, charge) for name, charge in equipment],
    )


@app.route("/tractors/add", methods=["GET", "POST"])
@role_required("owner")
def add_tractor():
    if request.method == "POST":
        try:
            values = listing_form_values()
            db = get_db()
            cursor = db.execute(
                """INSERT INTO tractor_listings
                   (owner_id, name, brand, model, location, phone, photo_url, services, hourly_rate, acre_rate)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session["user_id"], values["name"], values["brand"], values["model"],
                 values["location"], values["phone"], values["photo_url"], values["services"],
                 values["hourly_rate"], values["acre_rate"]),
            )
            save_equipment(db, cursor.lastrowid, values["equipment"])
            db.commit()
            flash("Tractor listing added.", "success")
            return redirect(url_for("tractor_details", listing_id=cursor.lastrowid))
        except ValueError as error:
            flash(str(error), "error")
    return render_template("add_tractor.html", listing=None, equipment_text="")


@app.route("/tractors/<int:listing_id>/edit", methods=["GET", "POST"])
@role_required("owner")
def edit_tractor(listing_id):
    listing = owned_listing(listing_id, session["user_id"])
    if listing is None:
        abort(404)
    db = get_db()
    equipment_rows = db.execute("SELECT * FROM equipment WHERE listing_id = ?", (listing_id,)).fetchall()
    equipment_text = "\n".join(f'{row["name"]} | {row["charge"]:.2f}' for row in equipment_rows)
    if request.method == "POST":
        try:
            values = listing_form_values()
            db.execute(
                """UPDATE tractor_listings SET name = ?, brand = ?, model = ?, location = ?,
                   phone = ?, photo_url = ?, services = ?, hourly_rate = ?, acre_rate = ?
                   WHERE id = ? AND owner_id = ?""",
                (values["name"], values["brand"], values["model"], values["location"],
                 values["phone"], values["photo_url"], values["services"], values["hourly_rate"],
                 values["acre_rate"], listing_id, session["user_id"]),
            )
            save_equipment(db, listing_id, values["equipment"])
            db.commit()
            flash("Listing updated. Existing booking prices remain unchanged.", "success")
            return redirect(url_for("tractor_details", listing_id=listing_id))
        except ValueError as error:
            flash(str(error), "error")
    return render_template("add_tractor.html", listing=listing, equipment_text=equipment_text)


@app.route("/tractors/<int:listing_id>/delete", methods=["POST"])
@role_required("owner")
def delete_tractor(listing_id):
    db = get_db()
    result = db.execute(
        "UPDATE tractor_listings SET active = 0 WHERE id = ? AND owner_id = ?",
        (listing_id, session["user_id"]),
    )
    if result.rowcount == 0:
        abort(404)
    db.commit()
    flash("Listing removed from search. Existing bookings and records are retained.", "success")
    return redirect(url_for("owner_dashboard"))


@app.route("/tractors/<int:listing_id>/availability", methods=["GET", "POST"])
@role_required("owner")
def manage_availability(listing_id):
    listing = owned_listing(listing_id, session["user_id"])
    if listing is None:
        abort(404)
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            day = request.form.get("available_date", "")
            start = request.form.get("start_time", "")
            end = request.form.get("end_time", "")
            try:
                parsed_day = date.fromisoformat(day)
                datetime.strptime(start, "%H:%M")
                datetime.strptime(end, "%H:%M")
                if parsed_day < date.today() or start >= end:
                    raise ValueError
                db.execute(
                    "INSERT INTO availability (listing_id, available_date, start_time, end_time) VALUES (?, ?, ?, ?)",
                    (listing_id, day, start, end),
                )
                db.commit()
                flash("Availability slot added.", "success")
            except ValueError:
                flash("Choose a future/current date and a valid start time before the end time.", "error")
            except sqlite3.IntegrityError as error:
                db.rollback()
                if "UNIQUE constraint" in str(error):
                    flash("This availability slot already exists.", "error")
                else:
                    raise
        elif action == "delete":
            db.execute(
                "DELETE FROM availability WHERE id = ? AND listing_id = ?",
                (request.form.get("slot_id"), listing_id),
            )
            db.commit()
            flash("Availability slot removed.", "success")
        return redirect(url_for("manage_availability", listing_id=listing_id))
    slots = db.execute(
        "SELECT * FROM availability WHERE listing_id = ? ORDER BY available_date, start_time",
        (listing_id,),
    ).fetchall()
    return render_template("availability.html", listing=listing, slots=slots)


def get_available_slots(listing_id, work_date):
    return get_db().execute(
        """SELECT * FROM availability
           WHERE listing_id = ? AND available_date = ? ORDER BY start_time""",
        (listing_id, work_date),
    ).fetchall()


def booking_conflict(listing_id, work_date, start_time, end_time, exclude_id=None):
    query = """SELECT id FROM bookings WHERE listing_id = ? AND work_date = ?
               AND status IN ('awaiting_payment', 'confirmed', 'in_progress')
               AND start_time < ? AND end_time > ?"""
    params = [listing_id, work_date, end_time, start_time]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return get_db().execute(query, params).fetchone() is not None


def validate_booking_window(listing_id, work_date, start_time, end_time):
    try:
        day = date.fromisoformat(work_date)
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")
    except ValueError:
        raise ValueError("Choose a valid date and start/end times.")
    if day < date.today() or (day == date.today() and start.time() <= datetime.now().time()):
        raise ValueError("The booking date and start time must be in the future.")
    if end <= start:
        raise ValueError("End time must be after start time.")
    slots = get_available_slots(listing_id, work_date)
    if not any(slot["start_time"] <= start_time and slot["end_time"] >= end_time for slot in slots):
        raise ValueError("That time is outside the owner's published availability.")
    if booking_conflict(listing_id, work_date, start_time, end_time):
        raise ValueError("That time overlaps an accepted or confirmed booking.")
    return Decimal(str((end - start).seconds / 3600)).quantize(Decimal("0.01"))


def api_error(message, status_code):
    return jsonify({"error": message}), status_code


def api_json_body():
    if not request.is_json:
        return None, api_error("Send a JSON request with Content-Type: application/json.", 400)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, api_error("The JSON request body must be an object.", 400)
    return body, None


def api_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            return api_error("A bearer access token is required.", 401)
        signer = URLSafeTimedSerializer(
            current_app.config["SECRET_KEY"], salt="agrirent-mobile-api"
        )
        try:
            payload = signer.loads(
                token.strip(), max_age=current_app.config["API_TOKEN_MAX_AGE"]
            )
        except SignatureExpired:
            return api_error("The access token has expired. Please log in again.", 401)
        except BadSignature:
            return api_error("The access token is invalid.", 401)
        if not isinstance(payload, dict) or not isinstance(payload.get("user_id"), int):
            return api_error("The access token is invalid.", 401)
        user = get_db().execute(
            "SELECT id, customer_code, name, email, phone, role FROM users WHERE id = ?",
            (payload["user_id"],),
        ).fetchone()
        if user is None or user["role"] != payload.get("role"):
            return api_error("The account for this access token is no longer available.", 401)
        g.api_user = user
        return view(*args, **kwargs)
    return wrapped


def api_listing_json(listing):
    db = get_db()
    equipment = db.execute(
        "SELECT id, name, charge FROM equipment WHERE listing_id = ? ORDER BY name",
        (listing["id"],),
    ).fetchall()
    availability = db.execute(
        """SELECT available_date, start_time, end_time FROM availability
           WHERE listing_id = ? AND available_date >= ? ORDER BY available_date, start_time""",
        (listing["id"], date.today().isoformat()),
    ).fetchall()
    return {
        "id": listing["id"],
        "name": listing["name"],
        "brand": listing["brand"],
        "model": listing["model"],
        "location": listing["location"],
        "phone": listing["phone"],
        "photo_url": listing["photo_url"],
        "services": listing["services"],
        "hourly_rate": listing["hourly_rate"],
        "acre_rate": listing["acre_rate"],
        "owner": {
            "name": listing["owner_name"],
            "phone": listing["phone"],
        },
        "equipment": [
            {"id": item["id"], "name": item["name"], "charge": item["charge"]}
            for item in equipment
        ],
        "availability": [dict(slot) for slot in availability],
    }


@app.route("/api/login", methods=["POST"])
def api_login():
    body, error = api_json_body()
    if error:
        return error
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return api_error("Email and password are required.", 400)
    user = get_db().execute(
        "SELECT id, customer_code, name, email, phone, password_hash, role FROM users WHERE email = ? COLLATE NOCASE",
        (email.strip(),),
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return api_error("Email or password was not recognized.", 401)
    token = URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt="agrirent-mobile-api"
    ).dumps({"user_id": user["id"], "role": user["role"]})
    return jsonify({
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": current_app.config["API_TOKEN_MAX_AGE"],
        "user": {
            "id": user["id"],
            "customer_id": user["customer_code"],
            "name": user["name"],
            "email": user["email"],
            "phone": user["phone"],
            "role": user["role"],
        },
    })


@app.route("/api/tractors", methods=["GET"])
def api_tractors():
    search = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    tractor_type = request.args.get("type", "").strip()
    work_date = request.args.get("work_date", "").strip()
    if work_date:
        try:
            date.fromisoformat(work_date)
        except ValueError:
            return api_error("work_date must use YYYY-MM-DD format.", 400)
    query = """SELECT l.*, u.name AS owner_name FROM tractor_listings l
               JOIN users u ON u.id = l.owner_id WHERE l.active = 1"""
    params = []
    if search:
        query += """ AND (l.name LIKE ? OR l.brand LIKE ? OR l.model LIKE ?
                          OR l.location LIKE ? OR EXISTS (
                              SELECT 1 FROM equipment e
                              WHERE e.listing_id = l.id AND e.name LIKE ?
                          ))"""
        params.extend([f"%{search}%"] * 5)
    if location:
        query += " AND l.location LIKE ?"
        params.append(f"%{location}%")
    if tractor_type:
        query += """ AND (l.name LIKE ? OR l.brand LIKE ? OR l.model LIKE ?
                          OR EXISTS (SELECT 1 FROM equipment e
                                     WHERE e.listing_id = l.id AND e.name LIKE ?))"""
        params.extend([f"%{tractor_type}%"] * 4)
    if work_date:
        query += """ AND EXISTS (SELECT 1 FROM availability a
                                 WHERE a.listing_id = l.id AND a.available_date = ?)"""
        params.append(work_date)
    query += " ORDER BY l.created_at DESC"
    rows = get_db().execute(query, params).fetchall()
    return jsonify({"tractors": [api_listing_json(row) for row in rows]})


@app.route("/api/tractors/<int:listing_id>", methods=["GET"])
def api_tractor_details(listing_id):
    listing = owned_listing(listing_id, active_only=True)
    if listing is None:
        return api_error("Tractor listing not found.", 404)
    return jsonify({"tractor": api_listing_json(listing)})


@app.route("/api/book", methods=["POST"])
@api_login_required
def api_book():
    if g.api_user["role"] != "customer":
        return api_error("Only customer accounts can create bookings.", 403)
    body, error = api_json_body()
    if error:
        return error
    listing_id = body.get("listing_id")
    if isinstance(listing_id, bool) or not isinstance(listing_id, int) or listing_id <= 0:
        return api_error("listing_id must be a positive integer.", 400)
    listing = owned_listing(listing_id, active_only=True)
    if listing is None:
        return api_error("Tractor listing not found.", 404)

    string_fields = ("work_date", "start_time", "end_time", "rental_mode", "payment_plan")
    if any(not isinstance(body.get(field), str) for field in string_fields):
        return api_error("Work date, start/end time, rental mode and payment plan are required.", 400)
    service = body.get("service", "")
    notes = body.get("notes", "")
    equipment_ids = body.get("equipment_ids", [])
    if not isinstance(service, str) or not isinstance(notes, str):
        return api_error("service and notes must be strings.", 400)
    if (not isinstance(equipment_ids, list) or len(equipment_ids) > 50 or
            any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in equipment_ids) or
            len(set(equipment_ids)) != len(equipment_ids)):
        return api_error("equipment_ids must be a list of unique positive integers.", 400)

    mode = body["rental_mode"]
    plan = body["payment_plan"]
    if mode not in ("hourly", "acre", "combined"):
        return api_error("rental_mode must be hourly, acre or combined.", 400)
    if plan not in ("advance", "full_advance", "after_work"):
        return api_error("payment_plan must be advance, full_advance or after_work.", 400)
    try:
        acres = parse_money(str(body.get("acres", "0")), "Land area")
        if mode in ("acre", "combined") and acres <= 0:
            raise ValueError("Enter land area greater than zero for per-acre pricing.")
        if mode == "hourly" and listing["hourly_rate"] <= 0:
            raise ValueError("Hourly pricing is not offered for this tractor.")
        if mode == "acre" and listing["acre_rate"] <= 0:
            raise ValueError("Per-acre pricing is not offered for this tractor.")
        if mode == "combined" and (listing["hourly_rate"] <= 0 or listing["acre_rate"] <= 0):
            raise ValueError("Combined pricing requires both rates to be configured.")

        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        hours = validate_booking_window(
            listing_id, body["work_date"], body["start_time"], body["end_time"]
        )
        selected_equipment = []
        if equipment_ids:
            placeholders = ",".join("?" for _ in equipment_ids)
            selected_equipment = db.execute(
                f"SELECT * FROM equipment WHERE listing_id = ? AND id IN ({placeholders})",
                [listing_id] + equipment_ids,
            ).fetchall()
            if len(selected_equipment) != len(equipment_ids):
                raise ValueError("One or more equipment IDs are not available for this tractor.")

        rental = Decimal("0.00")
        if mode in ("hourly", "combined"):
            rental += Decimal(str(listing["hourly_rate"])) * hours
        if mode in ("acre", "combined"):
            rental += Decimal(str(listing["acre_rate"])) * acres
        rental = rental.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        equipment_total = sum(
            (Decimal(str(item["charge"])) for item in selected_equipment),
            Decimal("0.00"),
        )
        total = rental + equipment_total
        if total <= 0:
            raise ValueError("The calculated booking price must be greater than zero.")
        total_cents = cents(total)
        advance_cents = max(
            1,
            cents(total * Decimal(str(current_app.config["ADVANCE_PERCENT"])) / 100),
        )
        cursor = db.execute(
            """INSERT INTO bookings
               (customer_id, listing_id, work_date, start_time, end_time, rental_mode, hours,
                acres, service, notes, payment_plan, advance_percent, hourly_rate, acre_rate,
                equipment_total_cents, total_cents, advance_cents, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested')""",
            (g.api_user["id"], listing_id, body["work_date"], body["start_time"],
             body["end_time"], mode, float(hours), float(acres), service.strip()[:500],
             notes.strip()[:1000], plan, current_app.config["ADVANCE_PERCENT"],
             listing["hourly_rate"], listing["acre_rate"], cents(equipment_total),
             total_cents, advance_cents),
        )
        booking_id = cursor.lastrowid
        db.executemany(
            """INSERT INTO booking_equipment
               (booking_id, equipment_id, equipment_name, charge) VALUES (?, ?, ?, ?)""",
            [(booking_id, item["id"], item["name"], item["charge"]) for item in selected_equipment],
        )
        db.commit()
    except ValueError as exc:
        get_db().rollback()
        return api_error(str(exc), 400)
    except sqlite3.IntegrityError:
        get_db().rollback()
        return api_error("The booking could not be saved. Check the submitted booking details.", 400)

    return jsonify({
        "booking": {
            "id": booking_id,
            "status": "requested",
            "listing_id": listing_id,
            "work_date": body["work_date"],
            "start_time": body["start_time"],
            "end_time": body["end_time"],
            "rental_mode": mode,
            "hours": float(hours),
            "acres": float(acres),
            "payment_plan": plan,
            "advance_percent": current_app.config["ADVANCE_PERCENT"],
            "rental_cents": cents(rental),
            "equipment_cents": cents(equipment_total),
            "total_cents": total_cents,
            "advance_cents": advance_cents,
            "currency": "INR",
            "equipment": [
                {"name": item["name"], "charge": item["charge"]}
                for item in selected_equipment
            ],
        }
    }), 201


@app.route("/api/my-bookings", methods=["GET"])
@api_login_required
def api_my_bookings():
    if g.api_user["role"] != "customer":
        return api_error("Only customer accounts have a personal booking list.", 403)
    rows = get_db().execute(
        """SELECT b.*, l.name AS tractor_name, l.brand, l.model, l.location,
                  owner.name AS owner_name, l.phone AS owner_phone
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users owner ON owner.id = l.owner_id
           WHERE b.customer_id = ? ORDER BY b.created_at DESC""",
        (g.api_user["id"],),
    ).fetchall()
    bookings = []
    for row in rows:
        equipment = get_db().execute(
            """SELECT equipment_name, charge FROM booking_equipment
               WHERE booking_id = ? ORDER BY equipment_name""",
            (row["id"],),
        ).fetchall()
        paid = verified_paid_cents(row["id"])
        bookings.append({
            "id": row["id"],
            "status": row["status"],
            "work_date": row["work_date"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "hours": row["hours"],
            "acres": row["acres"],
            "rental_mode": row["rental_mode"],
            "service": row["service"],
            "notes": row["notes"],
            "payment_plan": row["payment_plan"],
            "tractor": {
                "id": row["listing_id"],
                "name": row["tractor_name"],
                "brand": row["brand"],
                "model": row["model"],
                "location": row["location"],
            },
            "owner": {"name": row["owner_name"], "phone": row["owner_phone"]},
            "equipment": [
                {"name": item["equipment_name"], "charge": item["charge"]}
                for item in equipment
            ],
            "total_cents": row["total_cents"],
            "advance_cents": row["advance_cents"],
            "paid_cents": paid,
            "balance_cents": max(row["total_cents"] - paid, 0),
            "currency": "INR",
            "created_at": row["created_at"],
        })
    return jsonify({"bookings": bookings})


def booking_quote_payload(listing, listing_id, form, hours, acres, mode, plan,
                          selected_equipment, rental_cents, equipment_cents,
                          total_cents, advance_cents):
    return {
        "customer_id": session["user_id"],
        "listing_id": listing_id,
        "work_date": form.get("work_date", ""),
        "start_time": form.get("start_time", ""),
        "end_time": form.get("end_time", ""),
        "rental_mode": mode,
        "hours": str(hours),
        "acres": str(acres),
        "service": form.get("service", "").strip()[:500],
        "notes": form.get("notes", "").strip()[:1000],
        "payment_plan": plan,
        "hourly_rate": str(listing["hourly_rate"]),
        "acre_rate": str(listing["acre_rate"]),
        "equipment": sorted(
            [[item["id"], item["name"], str(item["charge"])] for item in selected_equipment]
        ),
        "rental_cents": rental_cents,
        "equipment_cents": equipment_cents,
        "total_cents": total_cents,
        "advance_cents": advance_cents,
    }


@app.route("/book/<int:listing_id>", methods=["GET", "POST"])
@role_required("customer")
def create_booking(listing_id):
    listing = owned_listing(listing_id, active_only=True)
    if listing is None:
        abort(404)
    db = get_db()
    equipment_rows = db.execute(
        "SELECT * FROM equipment WHERE listing_id = ? ORDER BY name", (listing_id,)
    ).fetchall()
    chosen_date = request.values.get("work_date", "")
    slots = get_available_slots(listing_id, chosen_date) if chosen_date else []
    form_values = request.form if request.method == "POST" else {}
    selected_equipment_ids = request.form.getlist("equipment_ids") if request.method == "POST" else []
    quote = None
    quote_token = ""
    if request.method == "POST":
        try:
            work_date = request.form.get("work_date", "")
            start_time = request.form.get("start_time", "")
            end_time = request.form.get("end_time", "")
            hours = validate_booking_window(listing_id, work_date, start_time, end_time)
            mode = request.form.get("rental_mode", "")
            if mode not in ("hourly", "acre", "combined"):
                raise ValueError("Choose a valid pricing method.")
            acres = parse_money(request.form.get("acres", "0"), "Land area")
            if mode in ("acre", "combined") and acres <= 0:
                raise ValueError("Enter land area greater than zero for per-acre pricing.")
            if mode == "hourly" and listing["hourly_rate"] <= 0:
                raise ValueError("Hourly pricing is not offered for this tractor.")
            if mode == "acre" and listing["acre_rate"] <= 0:
                raise ValueError("Per-acre pricing is not offered for this tractor.")
            if mode == "combined" and (listing["hourly_rate"] <= 0 or listing["acre_rate"] <= 0):
                raise ValueError("Combined pricing requires both rates to be configured.")
            chosen_ids = request.form.getlist("equipment_ids")
            selected_equipment = []
            if chosen_ids:
                if len(chosen_ids) > len(equipment_rows) or len(set(chosen_ids)) != len(chosen_ids):
                    raise ValueError("One or more selected equipment items are invalid.")
                placeholders = ",".join("?" for _ in chosen_ids)
                selected_equipment = db.execute(
                    f"SELECT * FROM equipment WHERE listing_id = ? AND id IN ({placeholders})",
                    [listing_id] + chosen_ids,
                ).fetchall()
                if len(selected_equipment) != len(set(chosen_ids)):
                    raise ValueError("One or more selected equipment items are invalid.")
            rental = Decimal("0.00")
            if mode in ("hourly", "combined"):
                rental += Decimal(str(listing["hourly_rate"])) * hours
            if mode in ("acre", "combined"):
                rental += Decimal(str(listing["acre_rate"])) * acres
            rental = rental.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            equipment_total = sum((Decimal(str(item["charge"])) for item in selected_equipment), Decimal("0.00"))
            total = rental + equipment_total
            if total <= 0:
                raise ValueError("The calculated booking price must be greater than zero.")
            plan = request.form.get("payment_plan", "")
            if plan not in ("advance", "full_advance", "after_work"):
                raise ValueError("Choose a valid payment arrangement.")
            total_cents = cents(total)
            advance_cents = max(
                1,
                cents(total * Decimal(str(app.config["ADVANCE_PERCENT"])) / 100),
            )
            rental_cents = cents(rental)
            equipment_cents = cents(equipment_total)
            quote_payload = booking_quote_payload(
                listing, listing_id, request.form, hours, acres, mode, plan,
                selected_equipment, rental_cents, equipment_cents, total_cents,
                advance_cents,
            )
            action = request.form.get("action")
            if action == "quote":
                quote = {
                    "rental_cents": rental_cents,
                    "equipment_cents": equipment_cents,
                    "total_cents": total_cents,
                    "advance_cents": advance_cents,
                }
                quote_token = quote_signer.dumps(quote_payload)
                return render_template(
                    "booking.html", listing=listing, equipment=equipment_rows,
                    slots=slots, selected_date=work_date, form_values=form_values,
                    selected_equipment_ids=selected_equipment_ids, quote=quote,
                    quote_token=quote_token,
                )
            if action != "create":
                raise ValueError("Calculate a price quote before submitting a booking.")
            try:
                submitted_quote = quote_signer.loads(request.form.get("quote_token", ""))
            except BadSignature:
                raise ValueError("The quote is missing or expired. Calculate a new quote before booking.")
            if submitted_quote != quote_payload:
                raise ValueError("The tractor rates, selected options or work details changed. Calculate a new quote.")
            cursor = db.execute(
                """INSERT INTO bookings
                   (customer_id, listing_id, work_date, start_time, end_time, rental_mode, hours,
                    acres, service, notes, payment_plan, advance_percent, hourly_rate, acre_rate,
                    equipment_total_cents, total_cents, advance_cents, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested')""",
                (session["user_id"], listing_id, work_date, start_time, end_time, mode, float(hours),
                 float(acres), request.form.get("service", "").strip()[:500],
                 request.form.get("notes", "").strip()[:1000], plan, app.config["ADVANCE_PERCENT"],
                 listing["hourly_rate"], listing["acre_rate"], cents(equipment_total), total_cents,
                 advance_cents),
            )
            booking_id = cursor.lastrowid
            db.executemany(
                """INSERT INTO booking_equipment (booking_id, equipment_id, equipment_name, charge)
                   VALUES (?, ?, ?, ?)""",
                [(booking_id, item["id"], item["name"], item["charge"]) for item in selected_equipment],
            )
            db.commit()
            flash("Booking request sent to the owner.", "success")
            return redirect(url_for("customer_booking", booking_id=booking_id))
        except ValueError as error:
            db.rollback()
            flash(str(error), "error")
            quote = None
            chosen_date = request.form.get("work_date", "")
            slots = get_available_slots(listing_id, chosen_date) if chosen_date else []
    return render_template(
        "booking.html", listing=listing, equipment=equipment_rows, slots=slots,
        selected_date=chosen_date, form_values=form_values,
        selected_equipment_ids=selected_equipment_ids, quote=quote,
        quote_token=quote_token,
    )


def payment_history_for(booking_id):
    return get_db().execute(
        "SELECT p.*, u.name AS payer_name FROM payments p JOIN users u ON u.id = p.payer_id WHERE p.booking_id = ? ORDER BY p.created_at",
        (booking_id,),
    ).fetchall()


def render_booking(booking, is_owner=False, customer_type=None):
    paid = verified_paid_cents(booking["id"])
    payments = payment_history_for(booking["id"])
    pending = sum(item["amount_cents"] for item in payments if item["status"] == "pending")
    equipment = get_db().execute(
        "SELECT * FROM booking_equipment WHERE booking_id = ? ORDER BY equipment_name",
        (booking["id"],),
    ).fetchall()
    return render_template(
        "booking_details.html", booking=booking, paid_cents=paid,
        balance_cents=max(booking["total_cents"] - paid, 0),
        required_cents=booking_required_cents(booking), payments=payments,
        payment_due_cents=max(payment_due_cents(booking, paid) - pending, 0),
        equipment=equipment, is_owner=is_owner, customer_type=customer_type,
    )


@app.route("/bookings/<int:booking_id>")
@role_required("customer")
def customer_booking(booking_id):
    booking = booking_for_customer(booking_id, session["user_id"])
    if booking is None:
        abort(404)
    return render_booking(booking)


@app.route("/owner/bookings/<int:booking_id>")
@role_required("owner")
def owner_booking(booking_id):
    booking = booking_for_owner(booking_id, session["user_id"])
    if booking is None:
        abort(404)
    customer_type = customer_classification(
        session["user_id"], booking["customer_id"], exclude_booking_id=booking_id
    )
    return render_booking(booking, is_owner=True, customer_type=customer_type)


@app.route("/owner/bookings/<int:booking_id>/decision", methods=["POST"])
@role_required("owner")
def booking_decision(booking_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    booking = booking_for_owner(booking_id, session["user_id"])
    if booking is None:
        abort(404)
    decision = request.form.get("decision")
    if booking["status"] != "requested":
        db.rollback()
        flash("Only new requests can be accepted or rejected.", "error")
    elif decision == "reject":
        db.execute("UPDATE bookings SET status = 'rejected' WHERE id = ?", (booking_id,))
        db.commit()
        flash("Booking request rejected.", "success")
    elif decision == "accept":
        if booking_conflict(booking["listing_id"], booking["work_date"], booking["start_time"],
                            booking["end_time"], exclude_id=booking_id):
            db.rollback()
            flash("This request now conflicts with another accepted booking. Reject it or contact the customer.", "error")
        else:
            new_status = "confirmed" if booking["payment_plan"] == "after_work" else "awaiting_payment"
            updated = db.execute(
                "UPDATE bookings SET status = ? WHERE id = ? AND status = 'requested'",
                (new_status, booking_id),
            )
            if updated.rowcount != 1:
                db.rollback()
                flash("This request has already been handled.", "error")
            else:
                db.commit()
                flash(
                    "Booking accepted. It is confirmed for payment after completion."
                    if new_status == "confirmed" else "Booking accepted. It will be confirmed when the agreed advance is verified.",
                    "success",
                )
    else:
        db.rollback()
        flash("Choose a valid decision.", "error")
    return redirect(url_for("owner_booking", booking_id=booking_id))


@app.route("/owner/bookings/<int:booking_id>/work-status", methods=["POST"])
@role_required("owner")
def update_work_status(booking_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    booking = booking_for_owner(booking_id, session["user_id"])
    if booking is None:
        abort(404)
    new_status = request.form.get("status")
    valid = {"confirmed": "in_progress", "in_progress": "completed"}
    if valid.get(booking["status"]) != new_status:
        db.rollback()
        flash("Work status must progress from Confirmed to In Progress to Completed.", "error")
    else:
        updated = db.execute(
            "UPDATE bookings SET status = ? WHERE id = ? AND status = ?",
            (new_status, booking_id, booking["status"]),
        )
        if updated.rowcount == 1:
            db.commit()
            flash(f"Work marked {new_status.replace('_', ' ')}.", "success")
        else:
            db.rollback()
            flash("The booking status changed; refresh the booking and try again.", "error")
    return redirect(url_for("owner_booking", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>/payment", methods=["GET", "POST"])
@role_required("customer")
def payment(booking_id):
    booking = booking_for_customer(booking_id, session["user_id"])
    if booking is None:
        abort(404)
    db = get_db()
    paid = verified_paid_cents(booking_id)
    outstanding = max(booking["total_cents"] - paid, 0)
    required = booking_required_cents(booking)
    due_now = payment_due_cents(booking, paid)
    if due_now <= 0:
        flash("No payment is due under this agreement at the current booking stage.", "error")
        return redirect(url_for("customer_booking", booking_id=booking_id))
    if request.method == "POST":
        try:
            db.execute("BEGIN IMMEDIATE")
            amount = cents(parse_money(request.form.get("amount", ""), "Payment amount", allow_zero=False))
            method = request.form.get("method", "")
            reference = request.form.get("transaction_ref", "").strip()[:120]
            if method not in ("cash", "upi", "demo_online"):
                raise ValueError("Choose a valid payment method.")
            current_booking = db.execute(
                """SELECT status, payment_plan, total_cents, advance_cents FROM bookings
                   WHERE id = ? AND customer_id = ?""",
                (booking_id, session["user_id"]),
            ).fetchone()
            if current_booking is None:
                abort(404)
            paid = verified_paid_cents(booking_id)
            outstanding = max(booking["total_cents"] - paid, 0)
            due_now = payment_due_cents(current_booking, paid)
            pending_row = db.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS amount FROM payments WHERE booking_id = ? AND status = 'pending'",
                (booking_id,),
            ).fetchone()
            if due_now <= 0:
                raise ValueError("No payment is due under this agreement at the current booking stage.")
            if amount + pending_row["amount"] > due_now:
                raise ValueError("Payment amount cannot exceed the amount currently due, including pending payments.")
            if method == "demo_online" and not reference:
                reference = "DEMO-" + secrets.token_hex(6).upper()
            if amount <= max(required - paid, 0):
                kind = "advance"
            elif paid == 0 and amount == booking["total_cents"]:
                kind = "full"
            else:
                kind = "balance"
            db.execute(
                """INSERT INTO payments
                   (booking_id, payer_id, amount_cents, method, payment_kind, transaction_ref, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (booking_id, session["user_id"], amount, method, kind, reference),
            )
            db.commit()
            flash("Payment submitted for owner verification. Demo online payment is not a real charge.", "success")
            return redirect(url_for("customer_booking", booking_id=booking_id))
        except ValueError as error:
            flash(str(error), "error")
            db.rollback()
    pending = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS amount FROM payments WHERE booking_id = ? AND status = 'pending'",
        (booking_id,),
    ).fetchone()["amount"]
    return render_template(
        "payment.html", booking=booking, paid_cents=paid,
        balance_cents=outstanding, required_cents=required, pending_cents=pending,
        payable_cents=max(due_now - pending, 0),
    )


@app.route("/owner/payments/<int:payment_id>/verify", methods=["POST"])
@role_required("owner")
def verify_payment(payment_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    payment_row = db.execute(
        """SELECT p.*, b.id AS booking_id, b.status AS booking_status, b.total_cents,
                  b.payment_plan, b.advance_cents, b.listing_id
           FROM payments p JOIN bookings b ON b.id = p.booking_id
           JOIN tractor_listings l ON l.id = b.listing_id
           WHERE p.id = ? AND l.owner_id = ?""",
        (payment_id, session["user_id"]),
    ).fetchone()
    if payment_row is None:
        abort(404)
    action = request.form.get("action")
    if payment_row["status"] != "pending":
        db.rollback()
        flash("This payment has already been reviewed.", "error")
    elif action not in ("verify", "reject"):
        db.rollback()
        flash("Choose a valid payment action.", "error")
    else:
        if action == "verify":
            already_paid = verified_paid_cents(payment_row["booking_id"])
            if already_paid + payment_row["amount_cents"] > payment_row["total_cents"]:
                db.rollback()
                flash("This payment would exceed the booking total and cannot be verified.", "error")
                return redirect(url_for("owner_booking", booking_id=payment_row["booking_id"]))
            db.execute(
                "UPDATE payments SET status = 'verified', verified_at = CURRENT_TIMESTAMP WHERE id = ?",
                (payment_id,),
            )
            if payment_row["booking_status"] == "awaiting_payment":
                updated_paid = already_paid + payment_row["amount_cents"]
                required = payment_row["total_cents"] if payment_row["payment_plan"] == "full_advance" else payment_row["advance_cents"]
                if updated_paid >= required:
                    db.execute(
                        "UPDATE bookings SET status = 'confirmed' WHERE id = ?",
                        (payment_row["booking_id"],),
                    )
            flash("Payment verified and added to the booking record.", "success")
        else:
            db.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (payment_id,))
            flash("Payment marked as rejected.", "success")
        db.commit()
    return redirect(url_for("owner_booking", booking_id=payment_row["booking_id"]))


@app.route("/owner")
@role_required("owner")
def owner_dashboard():
    db = get_db()
    owner_id = session["user_id"]
    listings = db.execute(
        "SELECT * FROM tractor_listings WHERE owner_id = ? ORDER BY created_at DESC",
        (owner_id,),
    ).fetchall()
    stats = db.execute(
        """SELECT COUNT(*) AS total,
           SUM(CASE WHEN status = 'requested' THEN 1 ELSE 0 END) AS requests,
           SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id WHERE l.owner_id = ?""",
        (owner_id,),
    ).fetchone()
    recent = db.execute(
        """SELECT b.id, b.work_date, b.status, b.total_cents, u.name AS customer_name,
                  l.name AS tractor_name FROM bookings b
           JOIN users u ON u.id = b.customer_id JOIN tractor_listings l ON l.id = b.listing_id
           WHERE l.owner_id = ? ORDER BY b.created_at DESC LIMIT 8""",
        (owner_id,),
    ).fetchall()
    return render_template("owner_dashboard.html", listings=listings, stats=stats, recent=recent)


@app.route("/customer")
@role_required("customer")
def customer_dashboard():
    db = get_db()
    bookings = db.execute(
        """SELECT b.*, l.name AS tractor_name, l.location, u.name AS owner_name
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = l.owner_id
           WHERE b.customer_id = ? ORDER BY b.created_at DESC LIMIT 10""",
        (session["user_id"],),
    ).fetchall()
    bookings = [
        dict(booking, equipment=db.execute(
            "SELECT equipment_name FROM booking_equipment WHERE booking_id = ? ORDER BY equipment_name",
            (booking["id"],),
        ).fetchall())
        for booking in bookings
    ]
    summary = db.execute(
        """SELECT COUNT(*) AS total,
           SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
           FROM bookings WHERE customer_id = ?""",
        (session["user_id"],),
    ).fetchone()
    return render_template("customer_dashboard.html", bookings=bookings, summary=summary)


@app.route("/owner/requests")
@role_required("owner")
def booking_requests():
    rows = get_db().execute(
        """SELECT b.*, l.name AS tractor_name, u.name AS customer_name, u.phone AS customer_phone
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = b.customer_id
           WHERE l.owner_id = ? AND b.status = 'requested' ORDER BY b.created_at""",
        (session["user_id"],),
    ).fetchall()
    return render_template("booking_requests.html", bookings=rows)


@app.route("/owner/history")
@role_required("owner")
def booking_history():
    rows = get_db().execute(
        """SELECT b.*, l.name AS tractor_name, u.name AS customer_name
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = b.customer_id
           WHERE l.owner_id = ? AND b.status != 'requested'
           ORDER BY b.work_date DESC, b.start_time DESC""",
        (session["user_id"],),
    ).fetchall()
    return render_template("booking_history.html", bookings=rows, owner_view=True)


@app.route("/customer/history")
@role_required("customer")
def customer_history():
    rows = get_db().execute(
        """SELECT b.*, l.name AS tractor_name, l.location, u.name AS owner_name
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = l.owner_id
           WHERE b.customer_id = ? ORDER BY b.work_date DESC, b.start_time DESC""",
        (session["user_id"],),
    ).fetchall()
    return render_template("booking_history.html", bookings=rows, owner_view=False)


@app.route("/owner/customers")
@role_required("owner")
def customer_history_list():
    rows = get_db().execute(
        """SELECT u.id, u.customer_code, u.name, u.phone, u.email,
                  COUNT(b.id) AS total_bookings,
                  SUM(CASE WHEN b.status = 'completed' THEN 1 ELSE 0 END) AS completed_jobs,
                  MAX(b.work_date) AS most_recent
           FROM bookings b JOIN users u ON u.id = b.customer_id
           JOIN tractor_listings l ON l.id = b.listing_id
           WHERE l.owner_id = ? GROUP BY u.id ORDER BY most_recent DESC""",
        (session["user_id"],),
    ).fetchall()
    return render_template("customer_history.html", customers=rows)


@app.route("/owner/customers/<int:customer_id>")
@role_required("owner")
def customer_details(customer_id):
    db = get_db()
    customer = db.execute(
        """SELECT u.id, u.customer_code, u.name, u.phone, u.email,
                  COUNT(b.id) AS total_bookings,
                  SUM(CASE WHEN b.status = 'completed' THEN 1 ELSE 0 END) AS completed_jobs,
                  SUM(CASE WHEN b.status = 'completed' THEN b.acres ELSE 0 END) AS completed_acres,
                  SUM(CASE WHEN b.status = 'completed' THEN b.total_cents ELSE 0 END) AS completed_charges,
                  MIN(b.work_date) AS first_booking, MAX(b.work_date) AS most_recent
           FROM users u JOIN bookings b ON b.customer_id = u.id
           JOIN tractor_listings l ON l.id = b.listing_id
           WHERE u.id = ? AND l.owner_id = ? GROUP BY u.id""",
        (customer_id, session["user_id"]),
    ).fetchone()
    if customer is None:
        abort(404)
    status = customer_classification(session["user_id"], customer_id)
    bookings = db.execute(
        """SELECT b.*, l.name AS tractor_name FROM bookings b
           JOIN tractor_listings l ON l.id = b.listing_id
           WHERE b.customer_id = ? AND l.owner_id = ? ORDER BY b.work_date DESC""",
        (customer_id, session["user_id"]),
    ).fetchall()
    payment_rows = db.execute(
        """SELECT p.*, b.id AS booking_number FROM payments p
           JOIN bookings b ON b.id = p.booking_id JOIN tractor_listings l ON l.id = b.listing_id
           WHERE b.customer_id = ? AND l.owner_id = ? ORDER BY p.created_at DESC""",
        (customer_id, session["user_id"]),
    ).fetchall()
    verified = sum(row["amount_cents"] for row in payment_rows if row["status"] == "verified")
    outstanding = sum(
        max(row["total_cents"] - verified_paid_cents(row["id"]), 0)
        for row in bookings if row["status"] in ("confirmed", "in_progress", "completed")
    )
    return render_template(
        "customer_details.html", customer=customer, customer_status=status,
        bookings=bookings, payments=payment_rows, verified_cents=verified,
        outstanding_cents=outstanding,
    )


@app.route("/owner/payments")
@role_required("owner")
def payment_history():
    rows = get_db().execute(
        """SELECT p.*, b.id AS booking_number, b.status AS booking_status,
                  u.name AS customer_name, l.name AS tractor_name
           FROM payments p JOIN bookings b ON b.id = p.booking_id
           JOIN users u ON u.id = b.customer_id JOIN tractor_listings l ON l.id = b.listing_id
           WHERE l.owner_id = ? ORDER BY p.created_at DESC""",
        (session["user_id"],),
    ).fetchall()
    return render_template("payment_history.html", payments=rows, owner_view=True)


@app.route("/customer/payments")
@role_required("customer")
def customer_payment_history():
    rows = get_db().execute(
        """SELECT p.*, b.id AS booking_number, b.status AS booking_status,
                  l.name AS tractor_name, u.name AS owner_name
           FROM payments p JOIN bookings b ON b.id = p.booking_id
           JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = l.owner_id
           WHERE b.customer_id = ? ORDER BY p.created_at DESC""",
        (session["user_id"],),
    ).fetchall()
    return render_template("payment_history.html", payments=rows, owner_view=False)


@app.route("/owner/earnings")
@role_required("owner")
def earnings():
    rows = get_db().execute(
        """SELECT b.id, b.work_date, b.total_cents, b.status, l.name AS tractor_name,
                  u.name AS customer_name, COALESCE(SUM(CASE WHEN p.status = 'verified'
                      THEN p.amount_cents ELSE 0 END), 0) AS paid_cents
           FROM bookings b JOIN tractor_listings l ON l.id = b.listing_id
           JOIN users u ON u.id = b.customer_id LEFT JOIN payments p ON p.booking_id = b.id
           WHERE l.owner_id = ? GROUP BY b.id ORDER BY b.work_date DESC""",
        (session["user_id"],),
    ).fetchall()
    return render_template("earnings.html", bookings=rows)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.errorhandler(400)
def bad_request(error):
    if request.path.startswith("/api/"):
        return api_error(getattr(error, "description", "The request could not be processed."), 400)
    return render_template("error.html", code=400, message=getattr(error, "description", "The request could not be processed.")), 400


@app.errorhandler(403)
def forbidden(_error):
    if request.path.startswith("/api/"):
        return api_error("You do not have permission to perform this action.", 403)
    return render_template("error.html", code=403, message="You do not have permission to view this page."), 403


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return api_error("API resource not found.", 404)
    return render_template("404.html"), 404


@app.errorhandler(405)
def method_not_allowed(error):
    if request.path.startswith("/api/"):
        return api_error("Method not allowed for this API resource.", 405)
    return error


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
