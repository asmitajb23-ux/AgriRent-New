"""SQLite setup and connection helpers for AgriRent."""

import sqlite3
from pathlib import Path

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code TEXT UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'customer')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tractor_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    location TEXT NOT NULL,
    phone TEXT NOT NULL,
    photo_url TEXT NOT NULL DEFAULT '',
    services TEXT NOT NULL DEFAULT '',
    hourly_rate REAL NOT NULL DEFAULT 0 CHECK (hourly_rate >= 0),
    acre_rate REAL NOT NULL DEFAULT 0 CHECK (acre_rate >= 0),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES tractor_listings(id),
    name TEXT NOT NULL,
    charge REAL NOT NULL DEFAULT 0 CHECK (charge >= 0),
    UNIQUE (listing_id, name)
);

CREATE TABLE IF NOT EXISTS availability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES tractor_listings(id),
    available_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    CHECK (start_time < end_time),
    UNIQUE (listing_id, available_date, start_time, end_time)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    listing_id INTEGER NOT NULL REFERENCES tractor_listings(id),
    work_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    rental_mode TEXT NOT NULL CHECK (rental_mode IN ('hourly', 'acre', 'combined')),
    hours REAL NOT NULL CHECK (hours > 0),
    acres REAL NOT NULL DEFAULT 0 CHECK (acres >= 0),
    service TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    payment_plan TEXT NOT NULL CHECK (payment_plan IN ('advance', 'full_advance', 'after_work')),
    advance_percent REAL NOT NULL,
    hourly_rate REAL NOT NULL,
    acre_rate REAL NOT NULL,
    equipment_total_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    advance_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'awaiting_payment', 'confirmed', 'rejected', 'in_progress', 'completed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS booking_equipment (
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    equipment_id INTEGER REFERENCES equipment(id) ON DELETE SET NULL,
    equipment_name TEXT NOT NULL,
    charge REAL NOT NULL,
    PRIMARY KEY (booking_id, equipment_name)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    payer_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    method TEXT NOT NULL CHECK (method IN ('cash', 'upi', 'demo_online')),
    payment_kind TEXT NOT NULL CHECK (payment_kind IN ('advance', 'balance', 'full')),
    transaction_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'rejected')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_listing_owner ON tractor_listings(owner_id);
CREATE INDEX IF NOT EXISTS idx_availability_date ON availability(listing_id, available_date);
CREATE INDEX IF NOT EXISTS idx_booking_customer ON bookings(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_booking_listing_time ON bookings(listing_id, work_date, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments(booking_id, status);
"""


def get_db():
    """Return this request's SQLite connection, creating it on first use."""
    if "db" not in g:
        db_path = Path(current_app.config["DATABASE"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(db_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create all tables and indexes. Safe to run every time the app starts."""
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
