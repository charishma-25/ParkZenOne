"""ParkZen API: manager-confirmed YOLO parking updates and customer availability."""
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
import calendar
from zoneinfo import ZoneInfo
import math
from functools import wraps
import os
from pathlib import Path
import secrets
import sqlite3
import json
import threading
import time
import hmac
import hashlib
import urllib.request
import urllib.error
from werkzeug.security import check_password_hash, generate_password_hash
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PARKZEN_DB", ROOT / "parkzen.sqlite3"))
UPLOAD_DIR = ROOT / "uploads"
MODEL_PATH = ROOT / "best.pt"
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
PAYMENT_HOLD_MINUTES = int(os.environ.get("PARKZEN_PAYMENT_HOLD_MINUTES", "10"))
LOCAL_ZONE = ZoneInfo(os.environ.get("PARKZEN_TIMEZONE", "Asia/Kolkata"))
MAX_BOOKING_HOURS = int(os.environ.get("PARKZEN_MAX_BOOKING_HOURS", "24"))
MAX_BOOKING_DAYS = int(os.environ.get("PARKZEN_MAX_BOOKING_DAYS", "30"))
OCCUPANCY_MAX_AGE_MINUTES = int(os.environ.get("PARKZEN_OCCUPANCY_MAX_AGE_MINUTES", "60"))
OVERSTAY_RATE_PER_MINUTE = max(0, int(os.environ.get("PARKZEN_OVERSTAY_RATE_PER_MINUTE", "1")))
OVERSTAY_MONITOR_INTERVAL_SECONDS = max(15, int(os.environ.get("PARKZEN_OVERSTAY_MONITOR_INTERVAL_SECONDS", "60")))
MAX_CUSTOMER_ACCOUNTS = max(1, int(os.environ.get("PARKZEN_MAX_CUSTOMER_ACCOUNTS", "300")))
DEFAULT_INACTIVITY_DAYS = max(1, int(os.environ.get("PARKZEN_INACTIVITY_DAYS", "90")))
LOYALTY_ACTIVE_DAYS_PER_WEEK = max(1, int(os.environ.get("PARKZEN_LOYALTY_DAYS_PER_WEEK", "1")))
LOYALTY_MISSED_WEEKS = max(0, int(os.environ.get("PARKZEN_LOYALTY_MISSED_WEEKS", "1")))
LOYALTY_ACTIVITY_ENDPOINTS = {
    "customer_history", "bookings", "customer_loyalty", "profile", "update_profile",
    "create_booking_hold", "verify_booking_payment",
}
CAPACITY_ALERT_THRESHOLDS = (80, 95, 100)
SMTP_HOST = os.environ.get("PARKZEN_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("PARKZEN_SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("PARKZEN_SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("PARKZEN_SMTP_PASSWORD", "")
CAPACITY_ALERT_FROM = os.environ.get("PARKZEN_ALERT_FROM", "")
CAPACITY_ALERT_TO = os.environ.get("PARKZEN_ADMIN_ALERT_EMAIL", "")
EMAIL_ALERTS_CONFIGURED = bool(SMTP_HOST and CAPACITY_ALERT_FROM and CAPACITY_ALERT_TO)
APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
configured_origins = [origin.strip() for origin in os.environ.get("PARKZEN_ORIGIN", "").split(",") if origin.strip()]
allowed_origins = configured_origins or ["http://localhost:5173", "http://127.0.0.1:5173", "http://[::1]:5173"]
CORS(APP, resources={r"/api/*": {"origins": allowed_origins}})
model = None

DESTINATIONS = [
    ("mall-a", "Mall A", "mall", "HITEC City, Hyderabad, Telangana", 17.4495, 78.3915, "Mall A"),
    ("mall-b", "Mall B", "mall", "Kukatpally, Hyderabad, Telangana", 17.4849, 78.3870, "Mall B"),
    ("mall-c", "Mall C", "mall", "Gachibowli, Hyderabad, Telangana", 17.4401, 78.3489, "Mall C"),
    ("restaurant-a", "Restaurant A", "restaurant", "Madhapur, Hyderabad, Telangana", 17.4483, 78.3915, "Restaurant A"),
    ("restaurant-b", "Restaurant B", "restaurant", "Jubilee Hills, Hyderabad, Telangana", 17.4310, 78.4070, "Restaurant B"),
    ("restaurant-c", "Restaurant C", "restaurant", "Banjara Hills, Hyderabad, Telangana", 17.4156, 78.4347, "Restaurant C"),
]

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

LOYALTY_TIERS = (
    {"key": "30-day", "label": "30-day", "months": 0, "days": 30, "discount_percent": 20},
    {"key": "6-month", "label": "6-month", "months": 6, "days": 0, "discount_percent": 50},
    {"key": "1-year", "label": "1-year", "months": 12, "days": 0, "discount_percent": 80},
)

def local_today():
    return datetime.now(LOCAL_ZONE).date()

def subtract_months(value, months):
    month_index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))

def add_months(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))

def qualifying_local_day():
    return datetime.now(LOCAL_ZONE).date().isoformat()

def record_customer_activity(conn, user_id):
    """Record trusted server-side usage once per local calendar day."""
    stamp = now()
    local_day = qualifying_local_day()
    conn.execute("UPDATE users SET last_active_at=? WHERE id=? AND role='customer' AND is_active=1", (stamp, user_id))
    conn.execute("""
        INSERT INTO loyalty_activity_days(user_id,local_date,first_active_at,last_active_at,qualifying_events)
        VALUES(?,?,?,?,1)
        ON CONFLICT(user_id,local_date) DO UPDATE SET
          last_active_at=excluded.last_active_at,
          qualifying_events=loyalty_activity_days.qualifying_events+1
    """, (user_id, local_day, stamp, stamp))

def loyalty_period_status(conn, user, tier, today=None):
    today = today or local_today()
    start = today - timedelta(days=tier["days"] - 1) if tier["days"] else subtract_months(today, tier["months"])
    created_at = parse_booking_timestamp(user["created_at"]).astimezone(LOCAL_ZONE).date()
    active_dates = [date.fromisoformat(row[0]) for row in conn.execute(
        "SELECT local_date FROM loyalty_activity_days WHERE user_id=? AND local_date>=? AND local_date<=? ORDER BY local_date",
        (user["id"], start.isoformat(), today.isoformat()),
    ).fetchall()]
    active_dates = sorted(set(active_dates))
    active_by_week = {}
    for active_day in active_dates:
        week_start = active_day - timedelta(days=active_day.weekday())
        active_by_week[week_start] = active_by_week.get(week_start, 0) + 1
    week_starts = set()
    cursor = start
    while cursor <= today:
        week_starts.add(cursor - timedelta(days=cursor.weekday()))
        cursor += timedelta(days=1)
    weeks_required = max(0, len(week_starts) - LOYALTY_MISSED_WEEKS)
    weeks_active = sum(1 for week_start in week_starts if active_by_week.get(week_start, 0) >= LOYALTY_ACTIVE_DAYS_PER_WEEK)
    tenure_met = created_at <= start
    eligible = tenure_met and weeks_active >= weeks_required
    return {
        "tier": tier["key"], "discount_percent": tier["discount_percent"],
        "period_start": start.isoformat(), "period_end": today.isoformat(),
        "account_age_days": max(0, (today-created_at).days), "tenure_met": tenure_met,
        "active_days": len(active_dates), "active_weeks": weeks_active,
        "required_weeks": weeks_required, "calendar_weeks": len(week_starts),
        "qualifying_days_per_week": LOYALTY_ACTIVE_DAYS_PER_WEEK,
        "grace_weeks": LOYALTY_MISSED_WEEKS, "eligible": eligible,
    }

def loyalty_summary(conn, user_id, today=None):
    user = conn.execute("SELECT id,created_at FROM users WHERE id=? AND role='customer'", (user_id,)).fetchone()
    if not user:
        return None
    today = today or local_today()
    periods = [loyalty_period_status(conn, user, tier, today) for tier in LOYALTY_TIERS]
    qualified = [period for period in periods if period["eligible"]]
    current = qualified[-1] if qualified else None
    next_tier = next((tier for tier in LOYALTY_TIERS if not current or tier["discount_percent"] > current["discount_percent"]), None)
    if next_tier:
        next_status = next(period for period in periods if period["tier"] == next_tier["key"])
        activity_progress = min(1.0, next_status["active_weeks"] / max(1, next_status["required_weeks"]))
        created_date = parse_booking_timestamp(user["created_at"]).astimezone(LOCAL_ZONE).date()
        tenure_date = created_date + timedelta(days=next_tier["days"] - 1) if next_tier["days"] else add_months(created_date, next_tier["months"])
        period_days = next_tier["days"] or max(1, (tenure_date-created_date).days)
        tenure_progress = min(1.0, ((today-created_date).days + 1) / period_days)
        next_status["progress_percent"] = round(min(activity_progress, tenure_progress) * 100, 1)
        next_status["weeks_remaining"] = max(0, next_status["required_weeks"] - next_status["active_weeks"])
        next_status["days_until_tenure"] = max(0, (tenure_date - today).days)
    else:
        next_status = None
    savings = conn.execute("""
        SELECT COALESCE(SUM(b.discount_amount_paise),0)
        FROM bookings b JOIN payments p ON p.booking_id=b.id
        WHERE b.customer_id=? AND p.status='verified' AND b.expiry_time<=?
    """, (user_id, now())).fetchone()[0]
    year_start = subtract_months(today, 12).isoformat()
    activity = conn.execute("""
        SELECT COUNT(*),COUNT(DISTINCT date(local_date,'weekday 0','-6 days'))
        FROM loyalty_activity_days WHERE user_id=? AND local_date>=? AND local_date<=?
    """, (user_id, year_start, today.isoformat())).fetchone()
    return {
        "current_tier": current["tier"] if current else "none",
        "tier_label": current["tier"] if current else "No tier yet",
        "discount_percent": current["discount_percent"] if current else 0,
        "eligible": bool(current), "periods": periods,
        "next_tier": next_tier["key"] if next_tier else None,
        "next_tier_progress": next_status,
        "qualifying_active_days_last_year": activity[0],
        "active_weeks_last_year": activity[1],
        "qualifying_days_per_week": LOYALTY_ACTIVE_DAYS_PER_WEEK,
        "grace_weeks": LOYALTY_MISSED_WEEKS,
        "savings_paise": savings,
    }

def discounted_booking_quote(base_rate, discount_percent, blocks):
    base_rate_paise = int((Decimal(str(base_rate)) * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    discounted_rate_paise = int((Decimal(base_rate_paise) * Decimal(100-discount_percent) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    total_paise = discounted_rate_paise * int(blocks)
    return {
        "base_rate_paise": base_rate_paise,
        "discounted_rate_paise": discounted_rate_paise,
        "discount_amount_paise": base_rate_paise * int(blocks) - total_paise,
        "total_amount_paise": total_paise,
    }

def active_customer_count(conn):
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='customer' AND is_active=1").fetchone()[0]

def check_capacity_alerts(conn):
    """Create one event per threshold crossing, rearming each threshold after capacity falls."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    count = active_customer_count(conn)
    stamp = now()
    created = []
    for threshold in CAPACITY_ALERT_THRESHOLDS:
        active = count * 100 >= MAX_CUSTOMER_ACCOUNTS * threshold
        state = conn.execute("SELECT active FROM capacity_alert_states WHERE threshold=?", (threshold,)).fetchone()
        if active and not state[0]:
            label = "full" if threshold == 100 else "critical" if threshold == 95 else "warning"
            message = (f"Customer account capacity is full ({count}/{MAX_CUSTOMER_ACCOUNTS})." if threshold == 100
                       else f"Customer account capacity reached {threshold}% ({count}/{MAX_CUSTOMER_ACCOUNTS}).")
            email_status = "pending" if EMAIL_ALERTS_CONFIGURED else "not_configured"
            cur = conn.execute("""
                INSERT INTO capacity_alert_events(threshold,customer_count,account_limit,message,email_status,created_at)
                VALUES(?,?,?,?,?,?)
            """, (threshold, count, MAX_CUSTOMER_ACCOUNTS, message, email_status, stamp))
            created.append(cur.lastrowid)
        conn.execute("UPDATE capacity_alert_states SET active=? WHERE threshold=?", (int(active), threshold))
    conn.commit()
    return created

def dispatch_capacity_alert_emails(conn):
    if not EMAIL_ALERTS_CONFIGURED:
        return
    import smtplib
    from email.message import EmailMessage
    pending = conn.execute("SELECT * FROM capacity_alert_events WHERE email_status='pending' ORDER BY id").fetchall()
    for event in pending:
        message = EmailMessage()
        message["Subject"] = f"ParkZen customer capacity alert ({event['threshold']}%)"
        message["From"] = CAPACITY_ALERT_FROM
        message["To"] = CAPACITY_ALERT_TO
        message.set_content(f"{event['message']}\nTriggered at {event['created_at']} UTC.")
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                smtp.starttls()
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)
            conn.execute("UPDATE capacity_alert_events SET email_status='sent' WHERE id=?", (event["id"],))
        except Exception:
            APP.logger.exception("Capacity alert email could not be sent (event %s)", event["id"])
            conn.execute("UPDATE capacity_alert_events SET email_status='failed' WHERE id=?", (event["id"],))
    conn.commit()

def slot_prefix(level_name):
    return "G" if level_name == "Ground Floor" else "B2" if level_name == "B2" else "B1" if level_name == "B1" else "L2" if level_name == "Level 2" else "L1"

def ensure_level_slots(conn, level_id, level_name, target_count):
    prefix = slot_prefix(level_name)
    existing_count = conn.execute("SELECT COUNT(*) FROM parking_slots WHERE level_id=?", (level_id,)).fetchone()[0]
    for index in range(existing_count, max(existing_count, target_count)):
        row_number, column_number = divmod(index, 10)
        row_number += 1
        column_number += 1
        slot_number = f"{prefix}{index + 1:02d}"
        conn.execute(
            "INSERT OR IGNORE INTO parking_slots(level_id,slot_number,row_number,column_number) VALUES(?,?,?,?)",
            (level_id, slot_number, row_number, column_number),
        )

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@APP.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection:
        connection.close()

def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, mobile TEXT NOT NULL DEFAULT '', password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'customer', destination_id TEXT REFERENCES destinations(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            notification_type TEXT NOT NULL,
            related_booking_id INTEGER REFERENCES bookings(id),
            reference TEXT,
            event_key TEXT NOT NULL UNIQUE,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS notifications_by_user_created ON notifications(user_id,created_at DESC,id DESC);
        CREATE TABLE IF NOT EXISTS destinations(id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, address TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, parking_ref TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS availability(destination_id TEXT NOT NULL REFERENCES destinations(id), level_id INTEGER PRIMARY KEY, total INTEGER NOT NULL, available INTEGER NOT NULL, occupied INTEGER NOT NULL, updated_at TEXT NOT NULL, manager_id INTEGER REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS predictions(id INTEGER PRIMARY KEY, destination_id TEXT NOT NULL, level_id INTEGER, manager_id INTEGER NOT NULL, image_path TEXT NOT NULL, total INTEGER NOT NULL, available INTEGER NOT NULL, occupied INTEGER NOT NULL, slots TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, destination_id TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS loyalty_activity_days(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            local_date TEXT NOT NULL,
            first_active_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            qualifying_events INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id,local_date)
        );
        CREATE TABLE IF NOT EXISTS location_pricing(destination_id TEXT PRIMARY KEY REFERENCES destinations(id), rate_per_block INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'INR', active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY,
            booking_code TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL REFERENCES users(id),
            destination_id TEXT NOT NULL REFERENCES destinations(id),
            level_id INTEGER NOT NULL REFERENCES parking_levels(id),
            slot_id INTEGER NOT NULL REFERENCES parking_slots(id),
            vehicle_type TEXT NOT NULL,
            start_time TEXT NOT NULL,
            expiry_time TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            rate_per_block INTEGER NOT NULL,
            blocks INTEGER NOT NULL,
            total_amount INTEGER NOT NULL,
            loyalty_tier TEXT NOT NULL DEFAULT 'none',
            base_rate_per_block INTEGER NOT NULL DEFAULT 0,
            discount_percent INTEGER NOT NULL DEFAULT 0,
            discount_amount_paise INTEGER NOT NULL DEFAULT 0,
            rate_per_block_paise INTEGER NOT NULL DEFAULT 0,
            total_amount_paise INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            hold_expires_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY,
            booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
            provider TEXT NOT NULL,
            provider_order_id TEXT,
            provider_payment_id TEXT,
            amount INTEGER NOT NULL,
            amount_paise INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL,
            method TEXT,
            status TEXT NOT NULL,
            verified_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slot_holds(
            id INTEGER PRIMARY KEY,
            slot_id INTEGER NOT NULL REFERENCES parking_slots(id),
            booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
            start_time TEXT NOT NULL,
            expiry_time TEXT NOT NULL,
            hold_expires_at TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS overstay_cases(
            id INTEGER PRIMARY KEY,
            booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
            slot_id INTEGER NOT NULL REFERENCES parking_slots(id),
            overstay_started_at TEXT NOT NULL,
            departure_detected_at TEXT,
            duration_minutes INTEGER,
            rate_per_minute INTEGER NOT NULL,
            estimated_penalty INTEGER NOT NULL DEFAULT 0,
            final_penalty INTEGER,
            calculation_at TEXT,
            status TEXT NOT NULL,
            penalty_payment_status TEXT NOT NULL DEFAULT 'not_due',
            departure_prediction_id INTEGER REFERENCES predictions(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS overstay_detection_events(
            id INTEGER PRIMARY KEY,
            case_id INTEGER NOT NULL REFERENCES overstay_cases(id),
            prediction_id INTEGER NOT NULL REFERENCES predictions(id),
            detected_status TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            UNIQUE(case_id,prediction_id)
        );
        CREATE TABLE IF NOT EXISTS penalty_payments(
            id INTEGER PRIMARY KEY,
            case_id INTEGER NOT NULL REFERENCES overstay_cases(id),
            provider TEXT NOT NULL,
            provider_order_id TEXT UNIQUE,
            provider_payment_id TEXT,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            method TEXT,
            status TEXT NOT NULL,
            verified_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_audit_log(
            id INTEGER PRIMARY KEY,
            actor_admin_id INTEGER NOT NULL REFERENCES users(id),
            affected_user_id INTEGER NOT NULL,
            affected_email TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS capacity_alert_states(
            threshold INTEGER PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS capacity_alert_events(
            id INTEGER PRIMARY KEY,
            threshold INTEGER NOT NULL,
            customer_count INTEGER NOT NULL,
            account_limit INTEGER NOT NULL,
            message TEXT NOT NULL,
            email_status TEXT NOT NULL DEFAULT 'not_configured',
            created_at TEXT NOT NULL
        );
        """)
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "destination_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN destination_id TEXT REFERENCES destinations(id)")
        if "is_active" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "last_active_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_active_at TEXT")
        conn.execute("UPDATE users SET last_active_at=created_at WHERE role='customer' AND last_active_at IS NULL")
        booking_columns = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
        loyalty_columns = {
            "loyalty_tier": "TEXT NOT NULL DEFAULT 'none'",
            "base_rate_per_block": "INTEGER NOT NULL DEFAULT 0",
            "discount_percent": "INTEGER NOT NULL DEFAULT 0",
            "discount_amount_paise": "INTEGER NOT NULL DEFAULT 0",
            "rate_per_block_paise": "INTEGER NOT NULL DEFAULT 0",
            "total_amount_paise": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in loyalty_columns.items():
            if column not in booking_columns:
                conn.execute(f"ALTER TABLE bookings ADD COLUMN {column} {definition}")
        conn.execute("UPDATE bookings SET base_rate_per_block=rate_per_block WHERE base_rate_per_block=0")
        conn.execute("UPDATE bookings SET rate_per_block_paise=CAST(ROUND(rate_per_block*100) AS INTEGER) WHERE rate_per_block_paise=0")
        conn.execute("UPDATE bookings SET total_amount_paise=CAST(ROUND(total_amount*100) AS INTEGER) WHERE total_amount_paise=0")
        payment_columns = {row[1] for row in conn.execute("PRAGMA table_info(payments)")}
        if "amount_paise" not in payment_columns:
            conn.execute("ALTER TABLE payments ADD COLUMN amount_paise INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE payments SET amount_paise=CAST(ROUND(amount*100) AS INTEGER) WHERE amount_paise=0")
        conn.execute("CREATE INDEX IF NOT EXISTS loyalty_days_by_user_date ON loyalty_activity_days(user_id,local_date)")
        legacy_days = {}
        for activity_row in conn.execute("SELECT a.user_id,a.created_at FROM activity a JOIN users u ON u.id=a.user_id WHERE u.role='customer' ORDER BY a.created_at,a.id"):
            try:
                active_at = parse_booking_timestamp(activity_row["created_at"])
            except (TypeError, ValueError, OverflowError):
                continue
            local_date = active_at.astimezone(LOCAL_ZONE).date().isoformat()
            key = (activity_row["user_id"], local_date)
            if key not in legacy_days:
                legacy_days[key] = [active_at.isoformat(timespec="seconds"), active_at.isoformat(timespec="seconds"), 1]
            else:
                legacy_days[key][1] = active_at.isoformat(timespec="seconds")
                legacy_days[key][2] += 1
        for (user_id, local_date), (first_active, last_active, events) in legacy_days.items():
            conn.execute("INSERT OR IGNORE INTO loyalty_activity_days(user_id,local_date,first_active_at,last_active_at,qualifying_events) VALUES(?,?,?,?,?)", (user_id, local_date, first_active, last_active, events))
        for threshold in CAPACITY_ALERT_THRESHOLDS:
            conn.execute("INSERT OR IGNORE INTO capacity_alert_states(threshold,active) VALUES(?,0)", (threshold,))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_manager_per_destination ON users(destination_id) WHERE role='manager' AND destination_id IS NOT NULL")
        for row in DESTINATIONS:
            conn.execute("INSERT OR IGNORE INTO destinations VALUES(?,?,?,?,?,?,?)", row)
        default_rates = {"mall-a": 5, "mall-b": 8, "mall-c": 6, "restaurant-a": 10, "restaurant-b": 7, "restaurant-c": 9}
        for destination_id, rate in default_rates.items():
            conn.execute(
                "INSERT OR IGNORE INTO location_pricing(destination_id,rate_per_block,currency,updated_at) VALUES(?,?,?,?)",
                (destination_id, rate, "INR", now()),
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS parking_levels(
                id INTEGER PRIMARY KEY,
                destination_id TEXT NOT NULL REFERENCES destinations(id),
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                UNIQUE(destination_id, name)
            )
        """)
        level_seed = {
            "mall-a": ["B2", "B1", "Ground Floor", "Level 1"],
            "mall-b": ["B1", "Ground Floor"],
            "mall-c": ["B2", "B1", "Ground Floor", "Level 1", "Level 2"],
            "restaurant-a": ["B1", "Ground Floor", "Level 1"],
            "restaurant-b": ["Ground Floor", "Level 1", "Level 2", "Level 3"],
            "restaurant-c": ["B1", "Ground Floor", "Level 1", "Level 2"],
        }
        for destination_id, levels in level_seed.items():
            for sort_order, level_name in enumerate(levels):
                conn.execute(
                    "INSERT OR IGNORE INTO parking_levels(destination_id,name,sort_order) VALUES(?,?,?)",
                    (destination_id, level_name, sort_order),
                )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS parking_slots(
                id INTEGER PRIMARY KEY,
                level_id INTEGER NOT NULL REFERENCES parking_levels(id),
                slot_number TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                column_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                updated_at TEXT,
                UNIQUE(level_id, slot_number),
                UNIQUE(level_id, row_number, column_number)
            )
        """)
        slot_columns = {row[1] for row in conn.execute("PRAGMA table_info(parking_slots)")}
        if "compatible_vehicle_types" not in slot_columns:
            conn.execute("ALTER TABLE parking_slots ADD COLUMN compatible_vehicle_types TEXT NOT NULL DEFAULT 'car,bike,ev_car'")
        level_rows = conn.execute("SELECT id,name FROM parking_levels ORDER BY id").fetchall()
        for level_id, level_name in level_rows:
            ensure_level_slots(conn, level_id, level_name, 40)
            prefix = slot_prefix(level_name)
            for slot in conn.execute("SELECT id,row_number,column_number FROM parking_slots WHERE level_id=?", (level_id,)).fetchall():
                expected = f"{prefix}{(slot['row_number'] - 1) * 10 + slot['column_number']:02d}"
                conn.execute("UPDATE parking_slots SET slot_number=? WHERE id=?", (expected, slot["id"]))
        availability_columns = {row[1] for row in conn.execute("PRAGMA table_info(availability)")}
        if "level_id" not in availability_columns:
            conn.execute("ALTER TABLE availability RENAME TO availability_legacy")
            conn.execute("""
                CREATE TABLE availability(
                    destination_id TEXT NOT NULL REFERENCES destinations(id),
                    level_id INTEGER PRIMARY KEY REFERENCES parking_levels(id),
                    total INTEGER NOT NULL,
                    available INTEGER NOT NULL,
                    occupied INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    manager_id INTEGER REFERENCES users(id)
                )
            """)
            conn.execute("""
                INSERT INTO availability(destination_id,level_id,total,available,occupied,updated_at,manager_id)
                SELECT legacy.destination_id,
                       (SELECT MIN(id) FROM parking_levels WHERE destination_id=legacy.destination_id),
                       legacy.total,legacy.available,legacy.occupied,legacy.updated_at,legacy.manager_id
                FROM availability_legacy AS legacy
            """)
            conn.execute("DROP TABLE availability_legacy")
        prediction_columns = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
        if "level_id" not in prediction_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN level_id INTEGER REFERENCES parking_levels(id)")
        conn.execute("""
            UPDATE predictions SET level_id=(
                SELECT MIN(id) FROM parking_levels WHERE parking_levels.destination_id=predictions.destination_id
            ) WHERE level_id IS NULL
        """)
        for prediction in conn.execute("SELECT id,level_id,total,slots,status FROM predictions WHERE level_id IS NOT NULL").fetchall():
            level = conn.execute("SELECT name FROM parking_levels WHERE id=?", (prediction["level_id"],)).fetchone()
            if not level:
                continue
            ensure_level_slots(conn, prediction["level_id"], level["name"], prediction["total"])
            if prediction["status"] != "confirmed":
                continue
            slots = json.loads(prediction["slots"] or "[]")
            if any(slot.get("slot_record_id") for slot in slots):
                continue
            slot_rows = conn.execute(
                "SELECT id FROM parking_slots WHERE level_id=? ORDER BY row_number,column_number,id",
                (prediction["level_id"],),
            ).fetchall()
            for slot_row in slot_rows:
                conn.execute("UPDATE parking_slots SET status='unknown' WHERE id=?", (slot_row["id"],))
            for slot_row, detected in zip(slot_rows, slots):
                conn.execute(
                    "UPDATE parking_slots SET status=?,updated_at=? WHERE id=?",
                    (detected.get("status", "unknown"), prediction["confirmed_at"] if "confirmed_at" in prediction.keys() else now(), slot_row["id"]),
                )
        if not conn.execute("SELECT 1 FROM users WHERE role='manager'").fetchone():
            conn.execute("INSERT INTO users(name,email,mobile,password,role,destination_id,created_at) VALUES(?,?,?,?,?,?,?)", ("Parking Manager", "manager@parkzen.local", "", generate_password_hash(os.environ.get("PARKZEN_MANAGER_PASSWORD", "1234")), "manager", "mall-a", now()))
        else:
            conn.execute("UPDATE users SET destination_id='mall-a' WHERE role='manager' AND destination_id IS NULL AND id=(SELECT MIN(id) FROM users WHERE role='manager')")
        if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            conn.execute("INSERT INTO users(name,email,mobile,password,role,created_at) VALUES(?,?,?,?,?,?)", ("ParkZen Administrator", "admin@parkzen.local", "", generate_password_hash(os.environ.get("PARKZEN_ADMIN_PASSWORD", "1234")), "admin", now()))

def load_model():
    global model
    if model is None:
        if not MODEL_PATH.exists():
            raise RuntimeError(f"ParkZen model file not found: {MODEL_PATH}")
        model = YOLO(str(MODEL_PATH))
    return model

def current_user():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token:
        return None
    return db().execute("""
        SELECT users.*,sessions.token FROM sessions JOIN users ON users.id=sessions.user_id
        WHERE sessions.token=? AND (users.role!='customer' OR users.is_active=1)
    """, (token,)).fetchone()

def iso_time(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

def billable_overstay_minutes(start, end):
    seconds = max(0, (end - start).total_seconds())
    return math.ceil(seconds / 60) if seconds else 0

def add_notification(conn, user_id, title, message, notification_type, event_key,
                    related_booking_id=None, reference=None):
    conn.execute("""
        INSERT OR IGNORE INTO notifications(user_id,title,message,notification_type,
            related_booking_id,reference,event_key,is_read,created_at)
        VALUES(?,?,?,?,?,?,?,0,?)
    """, (user_id, title, message, notification_type, related_booking_id,
          reference, event_key, now()))

def notify_overstay_state(conn, booking, case, previous_status, created=False):
    ref = f"overstay:{case['id']}"
    if created:
        add_notification(conn, booking["customer_id"], "Overstay monitoring started",
            f"Booking {booking['booking_code']} (slot {booking['slot_id']}) passed its reserved end time and is awaiting departure confirmation.",
            "overstay", f"{ref}:created", booking["id"], ref)
    if case["status"] != previous_status:
        add_notification(conn, booking["customer_id"], "Overstay status updated",
            f"Booking {booking['booking_code']} (slot {booking['slot_id']}) overstay status is now {case['status'].replace('_', ' ')}.",
            "overstay_update", f"{ref}:status:{case['status']}", booking["id"], ref)
    if case["final_penalty"] is not None and case["final_penalty"] > 0 and case["departure_detected_at"] and case["penalty_payment_status"] in ("pending", "not_due"):
        add_notification(conn, booking["customer_id"], "Overstay penalty ready to pay",
            f"The confirmed penalty for booking {booking['booking_code']} is ₹{case['final_penalty']}.",
            "penalty_due", f"{ref}:penalty-due", booking["id"], ref)

def create_due_duration_reminders(conn, current=None, customer_id=None):
    """Persist one reminder per active booking expiry, based only on saved timestamps."""
    current = current or datetime.now(timezone.utc)
    query = """
        SELECT b.id,b.customer_id,b.booking_code,b.start_time,b.expiry_time,b.created_at,
               d.name AS destination_name,ps.slot_number
        FROM bookings b JOIN destinations d ON d.id=b.destination_id
        JOIN parking_slots ps ON ps.id=b.slot_id
        WHERE b.status IN ('confirmed','active')
    """
    params = ()
    if customer_id is not None:
        query += " AND b.customer_id=?"
        params = (customer_id,)
    rows = conn.execute(query, params).fetchall()
    created = 0
    for booking in rows:
        try:
            starts = parse_booking_timestamp(booking["start_time"])
            expiry = parse_booking_timestamp(booking["expiry_time"])
            booked_at = parse_booking_timestamp(booking["created_at"])
        except (TypeError, ValueError, OverflowError):
            continue
        reminder_at = expiry - timedelta(minutes=10)
        if starts > current or current < reminder_at or current >= expiry or booked_at >= reminder_at:
            continue
        cursor = conn.execute("""
            INSERT OR IGNORE INTO notifications(user_id,title,message,notification_type,
                related_booking_id,reference,event_key,is_read,created_at)
            VALUES(?,?,?,?,?,?,?,0,?)
        """, (booking["customer_id"], "Parking Expires in 10 Minutes",
              f"Booking {booking['booking_code']} at {booking['destination_name']}, slot {booking['slot_number']}, expires at {expiry.astimezone(LOCAL_ZONE).strftime('%I:%M %p')}.",
              "duration_reminder", booking["id"], f"booking:{booking['id']}",
              f"booking:{booking['id']}:duration-reminder:{booking['expiry_time']}", now()))
        created += cursor.rowcount
    return created

def reconcile_overstays(conn):
    """Keep booking overstay records synchronized with the last confirmed slot evidence."""
    current = datetime.now(timezone.utc)
    current_iso = iso_time(current)
    expired_bookings = conn.execute("""
        SELECT b.*, s.status AS slot_status, s.updated_at AS slot_updated_at
        FROM bookings b JOIN parking_slots s ON s.id=b.slot_id
        WHERE b.status IN ('confirmed','active') AND b.expiry_time<=?
    """, (current_iso,)).fetchall()
    for booking in expired_bookings:
        case = conn.execute("SELECT * FROM overstay_cases WHERE booking_id=?", (booking["id"],)).fetchone()
        created_case = case is None
        if not case:
            # A post-expiry confirmed detection is required to resolve the case either way.
            # Until then, keep the slot unavailable and label any amount as an estimate.
            expiry = parse_booking_timestamp(booking["expiry_time"])
            minutes = billable_overstay_minutes(expiry, current)
            conn.execute("""
                INSERT OR IGNORE INTO overstay_cases(booking_id,slot_id,overstay_started_at,rate_per_minute,
                    estimated_penalty,status,penalty_payment_status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (booking["id"], booking["slot_id"], booking["expiry_time"],
                  OVERSTAY_RATE_PER_MINUTE, minutes * OVERSTAY_RATE_PER_MINUTE,
                  "awaiting_departure_confirmation", "not_due", current_iso, current_iso))
            case = conn.execute("SELECT * FROM overstay_cases WHERE booking_id=?", (booking["id"],)).fetchone()
        previous_status = case["status"]
        if case["status"] in ("completed", "penalty_pending", "penalty_paid"):
            notify_overstay_state(conn, booking, case, previous_status, created_case)
            continue
        prediction = conn.execute("""
            SELECT id,slots,created_at FROM predictions WHERE level_id=? AND status='confirmed' AND confirmed_at=?
            ORDER BY id DESC LIMIT 1
        """, (booking["level_id"], booking["slot_updated_at"])).fetchone()
        evidence_time = prediction["created_at"] if prediction else booking["slot_updated_at"]
        try:
            detection_at = parse_booking_timestamp(evidence_time)
            expiry = parse_booking_timestamp(booking["expiry_time"])
        except (TypeError, ValueError):
            continue
        if booking["slot_status"] == "available":
            # An available result during a booked session proves early departure; after expiry
            # it bounds the departure to the ML analysis time on the confirmed image.
            if detection_at < parse_booking_timestamp(booking["start_time"]):
                continue
            if detection_at < expiry and (current - detection_at) > timedelta(minutes=OCCUPANCY_MAX_AGE_MINUTES):
                continue
            minutes = billable_overstay_minutes(expiry, detection_at)
            final = minutes * OVERSTAY_RATE_PER_MINUTE
            conn.execute("""
                UPDATE overstay_cases SET departure_detected_at=?,duration_minutes=?,
                    estimated_penalty=?,final_penalty=?,calculation_at=?,status='completed',
                    penalty_payment_status=?,updated_at=? WHERE id=?
            """, (iso_time(detection_at), minutes, final, final, current_iso,
                  "not_due" if final == 0 else "pending", current_iso, case["id"]))
        elif booking["slot_status"] == "occupied" and detection_at >= expiry and (current - detection_at) <= timedelta(minutes=OCCUPANCY_MAX_AGE_MINUTES):
            minutes = billable_overstay_minutes(expiry, current)
            conn.execute("""
                UPDATE overstay_cases SET estimated_penalty=?,status='overstay',
                    penalty_payment_status='not_due',updated_at=? WHERE id=?
            """, (minutes * OVERSTAY_RATE_PER_MINUTE, current_iso, case["id"]))
        else:
            conn.execute("""
                UPDATE overstay_cases SET status='awaiting_departure_confirmation',updated_at=?
                WHERE id=? AND status='overstay'
            """, (current_iso, case["id"]))

        if prediction:
            for detected in json.loads(prediction["slots"] or "[]"):
                if detected.get("slot_record_id") != booking["slot_id"]:
                    continue
                conn.execute("""
                    INSERT OR IGNORE INTO overstay_detection_events(case_id,prediction_id,detected_status,detected_at)
                    VALUES(?,?,?,?)
                """, (case["id"], prediction["id"], detected.get("status", "unknown"), evidence_time))
                conn.execute("UPDATE overstay_cases SET departure_prediction_id=? WHERE id=? AND status='completed'", (prediction["id"], case["id"]))
                break
        case = conn.execute("SELECT * FROM overstay_cases WHERE id=?", (case["id"],)).fetchone()
        notify_overstay_state(conn, booking, case, previous_status, created_case)

def parse_booking_timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_ZONE)
    return parsed.astimezone(timezone.utc)

def cleanup_expired_holds(conn):
    timestamp = now()
    expired = conn.execute(
        "SELECT booking_id FROM slot_holds WHERE status='active' AND hold_expires_at<=?",
        (timestamp,),
    ).fetchall()
    for row in expired:
        conn.execute("UPDATE slot_holds SET status='expired' WHERE booking_id=?", (row["booking_id"],))
        conn.execute("UPDATE bookings SET status='expired' WHERE id=? AND status='payment_pending'", (row["booking_id"],))

def booking_json(row):
    item = dict(row)
    item["amount"] = item["total_amount"]
    item["payment_status"] = item.get("payment_status") or "pending"
    return item

def razorpay_order(amount, receipt, amount_paise=None):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return None, "Razorpay sandbox keys are not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET before starting the backend."
    amount_paise = amount_paise if amount_paise is not None else int((Decimal(str(amount)) * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    payload = json.dumps({"amount": amount_paise, "currency": "INR", "receipt": receipt, "payment_capture": 1}).encode()
    request = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    auth = (f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}").encode()
    import base64
    request.add_header("Authorization", f"Basic {base64.b64encode(auth).decode()}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read()), None
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as error:
        APP.logger.exception("Razorpay order creation failed")
        return None, f"Unable to create the Razorpay payment order: {error}"

def razorpay_payment(payment_id):
    import base64
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request(f"https://api.razorpay.com/v1/payments/{payment_id}", headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read()), None
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as error:
        APP.logger.exception("Razorpay payment lookup failed")
        return None, str(error)

def require_role(*roles):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify(error="Please sign in to continue."), 401
            if roles and user["role"] not in roles:
                return jsonify(error="You do not have access to this page."), 403
            g.user = user
            if user["role"] == "customer":
                changed = db().execute("SELECT 1 FROM users WHERE id=? AND is_active=1", (user["id"],)).fetchone()
                if not changed:
                    db().execute("DELETE FROM sessions WHERE token=?", (user["token"],))
                    db().commit()
                    return jsonify(error="This customer account is inactive. Contact an administrator."), 403
                if request.endpoint in LOYALTY_ACTIVITY_ENDPOINTS:
                    record_customer_activity(db(), user["id"])
                db().commit()
            return fn(*args, **kwargs)
        return wrapped
    return decorate

def user_json(user):
    item = {"id": user["id"], "name": user["name"], "email": user["email"], "mobile": user["mobile"], "role": user["role"], "destination_id": user["destination_id"]}
    if user["destination_id"]:
        destination = db().execute("SELECT name FROM destinations WHERE id=?", (user["destination_id"],)).fetchone()
        item["destination_name"] = destination["name"] if destination else None
    else:
        item["destination_name"] = None
    return item

def destination_json(row):
    reconcile_overstays(db())
    db().commit()
    item = dict(row)
    pricing = db().execute("SELECT rate_per_block,currency FROM location_pricing WHERE destination_id=? AND active=1", (item["id"],)).fetchone()
    item["pricing"] = dict(pricing) if pricing else None
    levels = db().execute("""
        SELECT parking_levels.id, parking_levels.name, parking_levels.sort_order,
               availability.total, availability.available, availability.occupied,
               availability.updated_at
        FROM parking_levels
        LEFT JOIN availability ON availability.level_id=parking_levels.id
        WHERE parking_levels.destination_id=?
        ORDER BY parking_levels.sort_order, parking_levels.id
    """, (item["id"],)).fetchall()
    item["levels"] = []
    observed_at = now()
    for level in levels:
        level_item = dict(level)
        slots = db().execute("""
            SELECT parking_slots.id,parking_slots.slot_number,parking_slots.row_number,parking_slots.column_number,
                   parking_slots.status,parking_slots.updated_at,parking_slots.compatible_vehicle_types,
                   EXISTS(
                       SELECT 1 FROM bookings AS b
                       WHERE b.slot_id=parking_slots.id AND b.start_time<=? AND b.expiry_time>?
                         AND (b.status IN ('confirmed','active') OR
                              (b.status='payment_pending' AND b.hold_expires_at>?))
                   ) OR EXISTS(
                       SELECT 1 FROM overstay_cases AS oc
                       WHERE oc.slot_id=parking_slots.id AND oc.status IN ('awaiting_departure_confirmation','overstay')
                   ) AS reserved_now,
                   (SELECT MIN(b.expiry_time) FROM bookings AS b
                    WHERE b.slot_id=parking_slots.id AND b.start_time<=? AND b.expiry_time>?
                      AND (b.status IN ('confirmed','active') OR
                           (b.status='payment_pending' AND b.hold_expires_at>?))) AS reservation_expiry_time
            FROM parking_slots
            WHERE parking_slots.level_id=?
            ORDER BY parking_slots.row_number,parking_slots.column_number,parking_slots.id
        """, (observed_at, observed_at, observed_at, observed_at, observed_at, observed_at, level["id"])).fetchall()
        level_item["slots"] = []
        for slot in slots:
            slot_item = dict(slot)
            try:
                fresh = bool(slot_item["updated_at"] and parse_booking_timestamp(slot_item["updated_at"]) >= datetime.now(timezone.utc) - timedelta(minutes=OCCUPANCY_MAX_AGE_MINUTES))
            except (TypeError, ValueError):
                fresh = False
            slot_item["display_status"] = "occupied" if slot_item["reserved_now"] else (slot_item["status"] if fresh else "unknown")
            level_item["slots"].append(slot_item)
        item["levels"].append(level_item)
    updates = [level for level in item["levels"] if level["updated_at"]]
    total = sum(level["total"] for level in updates)
    available = sum(level["available"] for level in updates)
    occupied = sum(level["occupied"] for level in updates)
    item["availability"] = {"total": total, "available": available, "occupied": occupied, "updated_at": max(level["updated_at"] for level in updates)} if updates else None
    item["status"] = "No recent update" if not updates else ("Parking full" if available == 0 else "Limited parking" if available / max(1, total) <= 0.2 else "Parking available")
    return item

@APP.get("/api/health")
def health():
    return jsonify(
        status="ok",
        model="configured" if MODEL_PATH.exists() else "missing",
        razorpay_configured=bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET),
    )

@APP.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    name, email, mobile, password = (str(data.get(key, "")).strip() for key in ("name", "email", "mobile", "password"))
    if not name or "@" not in email or len(mobile) < 7 or len(password) < 8:
        return jsonify(error="Enter your name, a valid email, mobile number, and a password of at least 8 characters."), 400
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT 1 FROM users WHERE email=?", (email.lower(),)).fetchone()
        if existing:
            conn.rollback()
            return jsonify(error="This email is already associated with an account. Sign in or contact an administrator if the account is inactive."), 409
        count = active_customer_count(conn)
        if count >= MAX_CUSTOMER_ACCOUNTS:
            conn.rollback()
            return jsonify(error=f"ParkZen has reached its customer account limit ({MAX_CUSTOMER_ACCOUNTS}). Please contact the administrator."), 409
        stamp = now()
        conn.execute("""
            INSERT INTO users(name,email,mobile,password,role,created_at,is_active,last_active_at)
            VALUES(?,?,?,?,?,?,1,?)
        """, (name, email.lower(), mobile, generate_password_hash(password), "customer", stamp, stamp))
        conn.commit()
        check_capacity_alerts(conn)
        return jsonify(message="Account created. Please sign in."), 201
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify(error="This email is already associated with an account. Sign in or contact an administrator if the account is inactive."), 409

@APP.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    user = db().execute("SELECT * FROM users WHERE email=?", (str(data.get("email", "")).strip().lower(),)).fetchone()
    if not user or not check_password_hash(user["password"], str(data.get("password", ""))):
        return jsonify(error="Email or password is incorrect."), 401
    if user["role"] == "customer" and not user["is_active"]:
        return jsonify(error="This customer account has been deactivated. Contact an administrator."), 403
    if data.get("role") and data["role"] != user["role"]:
        return jsonify(error=f"This account does not have {data['role']} access."), 403
    token = secrets.token_urlsafe(32)
    stamp = now()
    if user["role"] == "customer":
        record_customer_activity(db(), user["id"])
    db().execute("INSERT INTO sessions VALUES(?,?,?)", (token, user["id"], stamp))
    db().commit()
    return jsonify(token=token, user=user_json(user))

@APP.post("/api/auth/logout")
@require_role()
def logout():
    db().execute("DELETE FROM sessions WHERE token=?", (g.user["token"],)); db().commit()
    return jsonify(message="Signed out")

@APP.get("/api/auth/me")
@require_role()
def me():
    return jsonify(user=user_json(g.user))

@APP.get("/api/destinations")
def destinations():
    user = current_user()
    if user and user["role"] == "manager":
        if not user["destination_id"]:
            return jsonify(destinations=[])
        row = db().execute("SELECT * FROM destinations WHERE id=?", (user["destination_id"],)).fetchone()
        return jsonify(destinations=[destination_json(row)] if row else [])
    term = f"%{request.args.get('q','').strip()}%"
    kind = request.args.get("type", "all")
    rows = db().execute("SELECT * FROM destinations WHERE (name LIKE ? OR address LIKE ?) AND (?='all' OR type=?) ORDER BY name", (term, term, kind, kind)).fetchall()
    return jsonify(destinations=[destination_json(row) for row in rows])

@APP.get("/api/destinations/<destination_id>")
def destination_detail(destination_id):
    row = db().execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
    if not row: return jsonify(error="Destination not found."), 404
    user = current_user()
    if user and user["role"] == "manager" and user["destination_id"] != destination_id:
        return jsonify(error="You can only access your assigned parking location."), 403
    item = destination_json(row)
    if user and user["role"] == "customer":
        db().execute("INSERT INTO activity(user_id,destination_id,action,created_at) VALUES(?,?,?,?)", (user["id"], destination_id, "Viewed parking", now())); db().commit()
        record_customer_activity(db(), user["id"]); db().commit()
    return jsonify(destination=item)

@APP.get("/api/history")
@require_role("customer")
def customer_history():
    rows = db().execute("SELECT activity.created_at,activity.action,destinations.* FROM activity JOIN destinations ON destinations.id=activity.destination_id WHERE user_id=? ORDER BY activity.id DESC LIMIT 50", (g.user["id"],)).fetchall()
    return jsonify(history=[dict(row) for row in rows])

@APP.get("/api/manager/predictions")
@require_role("manager")
def prediction_history():
    rows = db().execute("SELECT predictions.*,destinations.name AS destination_name,parking_levels.name AS level_name FROM predictions JOIN destinations ON destinations.id=predictions.destination_id JOIN parking_levels ON parking_levels.id=predictions.level_id WHERE status='confirmed' AND manager_id=? ORDER BY id DESC LIMIT 50", (g.user["id"],)).fetchall()
    return jsonify(history=[dict(row) for row in rows])

@APP.get("/api/manager/levels")
@require_role("manager")
def manager_levels():
    rows = db().execute(
        "SELECT id,name,sort_order FROM parking_levels WHERE destination_id=? ORDER BY sort_order,id",
        (g.user["destination_id"],),
    ).fetchall()
    levels = []
    for row in rows:
        item = dict(row)
        item["slots"] = [dict(slot) for slot in db().execute(
            "SELECT id,slot_number,row_number,column_number,status,updated_at FROM parking_slots WHERE level_id=? ORDER BY row_number,column_number,id",
            (row["id"],),
        ).fetchall()]
        levels.append(item)
    return jsonify(levels=levels)

def map_detections_to_slots(level_id, detections, image_width, image_height):
    slots = [dict(row) for row in db().execute(
        "SELECT id,slot_number,row_number,column_number FROM parking_slots WHERE level_id=? ORDER BY id",
        (level_id,),
    ).fetchall()]
    if not slots or not image_width or not image_height:
        return detections
    used = set()
    row_count = max(slot["row_number"] for slot in slots)
    column_count = max(slot["column_number"] for slot in slots)
    for detection in detections:
        x1, y1, x2, y2 = detection["coordinates"]
        center_x = ((x1 + x2) / 2) / image_width
        center_y = ((y1 + y2) / 2) / image_height
        candidate = min(
            (slot for slot in slots if slot["id"] not in used),
            key=lambda slot: ((center_y - (slot["row_number"] - 0.5) / row_count) ** 2 + (center_x - (slot["column_number"] - 0.5) / column_count) ** 2),
            default=None,
        )
        if candidate:
            used.add(candidate["id"])
            detection["slot_id"] = candidate["slot_number"]
            detection["slot_record_id"] = candidate["id"]
    return detections

@APP.post("/api/manager/analyze")
@require_role("manager")
def analyze():
    destination_id = g.user["destination_id"]
    level_id = request.form.get("level_id", type=int)
    image = request.files.get("image")
    destination = db().execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
    if not destination: return jsonify(error="Your account is not assigned to a parking location. Contact an administrator."), 403
    level = db().execute("SELECT * FROM parking_levels WHERE id=? AND destination_id=?", (level_id, destination_id)).fetchone()
    if not level: return jsonify(error="Choose a valid parking level for your assigned location."), 400
    if not image or not image.filename: return jsonify(error="Choose a parking image to analyze."), 400
    if Path(image.filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}: return jsonify(error="Upload a JPG, PNG, or WebP image."), 400
    path = UPLOAD_DIR / f"{secrets.token_hex(12)}{Path(image.filename).suffix.lower()}"
    image.save(path)
    try:
        detections = []
        image_width = image_height = 0
        for result in load_model()(str(path), conf=0.25):
            image_height, image_width = result.orig_shape[:2]
            for box in result.boxes:
                class_id = int(box.cls[0])
                if class_id in (0, 1):
                    detections.append({"status": "available" if class_id == 0 else "occupied", "confidence": round(float(box.conf[0]), 4), "coordinates": [round(float(x), 2) for x in box.xyxy[0].tolist()]})
        detections.sort(key=lambda item: (item["coordinates"][1], item["coordinates"][0]))
        ensure_level_slots(db(), level_id, level["name"], len(detections))
        map_detections_to_slots(level_id, detections, image_width, image_height)
        available = sum(slot["status"] == "available" for slot in detections)
        occupied = sum(slot["status"] == "occupied" for slot in detections)
        cur = db().execute("INSERT INTO predictions(destination_id,level_id,manager_id,image_path,total,available,occupied,slots,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (destination_id, level_id, g.user["id"], path.name, len(detections), available, occupied, json.dumps(detections), "pending", now()))
        db().commit()
        return jsonify(prediction_id=cur.lastrowid, destination_id=destination_id, destination_name=destination["name"], level_id=level_id, level_name=level["name"], total=len(detections), available=available, occupied=occupied, slots=detections), 201
    except Exception as error:
        path.unlink(missing_ok=True)
        APP.logger.exception("YOLO analysis failed")
        return jsonify(error=f"AI analysis failed: {error}"), 500

@APP.post("/api/manager/confirm/<int:prediction_id>")
@require_role("manager")
def confirm(prediction_id):
    row = db().execute("SELECT * FROM predictions WHERE id=? AND status='pending' AND manager_id=?", (prediction_id, g.user["id"])).fetchone()
    if not row: return jsonify(error="This analysis is no longer awaiting confirmation."), 404
    confirmed = now()
    db().execute("UPDATE predictions SET status='confirmed',confirmed_at=? WHERE id=?", (confirmed, prediction_id))
    db().execute("INSERT INTO availability(destination_id,level_id,total,available,occupied,updated_at,manager_id) VALUES(?,?,?,?,?,?,?) ON CONFLICT(level_id) DO UPDATE SET total=excluded.total,available=excluded.available,occupied=excluded.occupied,updated_at=excluded.updated_at,manager_id=excluded.manager_id", (row["destination_id"], row["level_id"], row["total"], row["available"], row["occupied"], confirmed, g.user["id"]))
    db().execute("UPDATE parking_slots SET status='unknown',updated_at=? WHERE level_id=?", (confirmed, row["level_id"]))
    for slot in json.loads(row["slots"]):
        if slot.get("slot_record_id"):
            db().execute(
                "UPDATE parking_slots SET status=?,updated_at=? WHERE id=? AND level_id=?",
                (slot["status"], confirmed, slot["slot_record_id"], row["level_id"]),
            )
    reconcile_overstays(db())
    for detection in json.loads(row["slots"]):
        slot_record_id = detection.get("slot_record_id")
        if not slot_record_id:
            continue
        cases = db().execute("""
            SELECT oc.id FROM overstay_cases oc JOIN bookings b ON b.id=oc.booking_id
            WHERE oc.slot_id=? AND b.expiry_time<=?
        """, (slot_record_id, confirmed)).fetchall()
        for overstay_case in cases:
            db().execute("""
                INSERT OR IGNORE INTO overstay_detection_events(case_id,prediction_id,detected_status,detected_at)
                VALUES(?,?,?,?)
            """, (overstay_case["id"], prediction_id, detection["status"], confirmed))
            db().execute("UPDATE overstay_cases SET departure_prediction_id=? WHERE id=? AND ?='available' AND status='completed'", (prediction_id, overstay_case["id"], detection["status"]))
    db().commit()
    return jsonify(message="Parking availability updated.", destination_id=row["destination_id"], updated_at=confirmed)

@APP.get("/api/profile")
@require_role()
def profile():
    return jsonify(user=user_json(g.user))

@APP.get("/api/notifications")
@require_role("customer")
def customer_notifications():
    status = request.args.get("filter", "all")
    if status not in {"all", "read", "unread"}:
        return jsonify(error="Choose a valid notification filter."), 400
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        return jsonify(error="Invalid notification page."), 400
    clauses = ["user_id=?"]
    params = [g.user["id"]]
    if status == "read": clauses.append("is_read=1")
    if status == "unread": clauses.append("is_read=0")
    where = " AND ".join(clauses)
    conn = db()
    create_due_duration_reminders(conn, customer_id=g.user["id"])
    conn.commit()
    rows = conn.execute(f"SELECT id,title,message,notification_type,related_booking_id,reference,is_read,created_at FROM notifications WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM notifications WHERE {where}", params).fetchone()[0]
    unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (g.user["id"],)).fetchone()[0]
    return jsonify(notifications=[dict(row) for row in rows], total=total, unread_count=unread)

@APP.get("/api/notifications/unread-count")
@require_role("customer")
def customer_notification_count():
    conn = db()
    create_due_duration_reminders(conn, customer_id=g.user["id"])
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (g.user["id"],)).fetchone()[0]
    return jsonify(unread_count=count)

@APP.patch("/api/notifications/read-all")
@require_role("customer")
def mark_all_notifications_read():
    db().execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (g.user["id"],))
    db().commit()
    return jsonify(message="All notifications marked as read.", unread_count=0)

@APP.patch("/api/notifications/<int:notification_id>/read")
@require_role("customer")
def mark_notification_read(notification_id):
    cursor = db().execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (notification_id, g.user["id"]))
    db().commit()
    if not cursor.rowcount:
        return jsonify(error="Notification not found."), 404
    return jsonify(message="Notification marked as read.")

@APP.get("/api/bookings")
@require_role("customer")
def bookings():
    cleanup_expired_holds(db())
    reconcile_overstays(db())
    rows = db().execute("""
        SELECT bookings.*, destinations.name AS destination_name, parking_levels.name AS level_name,
               parking_slots.slot_number, payments.status AS payment_status,
               payments.provider_payment_id, payments.method
        FROM bookings
        JOIN destinations ON destinations.id=bookings.destination_id
        JOIN parking_levels ON parking_levels.id=bookings.level_id
        JOIN parking_slots ON parking_slots.id=bookings.slot_id
        LEFT JOIN payments ON payments.booking_id=bookings.id
        WHERE bookings.customer_id=?
        ORDER BY bookings.id DESC
    """, (g.user["id"],)).fetchall()
    db().commit()
    return jsonify(bookings=[booking_json(row) for row in rows])

@APP.get("/api/loyalty")
@require_role("customer")
def customer_loyalty():
    summary = loyalty_summary(db(), g.user["id"])
    return jsonify(loyalty=summary)

@APP.get("/api/admin/loyalty")
@require_role("admin")
def admin_loyalty():
    conn = db()
    customers = conn.execute("SELECT id,name,email,is_active,created_at FROM users WHERE role='customer' ORDER BY name,email").fetchall()
    result = []
    for customer in customers:
        summary = loyalty_summary(conn, customer["id"])
        result.append({**dict(customer), **summary})
    return jsonify(customers=result, rule={
        "active_days_per_week": LOYALTY_ACTIVE_DAYS_PER_WEEK,
        "grace_weeks": LOYALTY_MISSED_WEEKS,
        "timezone": str(LOCAL_ZONE),
    })

def overstay_rows(where_sql, params):
    rows = db().execute(f"""
        SELECT oc.*, b.booking_code,b.customer_id,b.destination_id,b.level_id,b.start_time,b.expiry_time,
               d.name AS destination_name,pl.name AS level_name,ps.slot_number,
               ps.status AS current_occupancy,ps.updated_at AS occupancy_updated_at,
               COALESCE((SELECT pp.status FROM penalty_payments pp WHERE pp.case_id=oc.id ORDER BY pp.id DESC LIMIT 1),oc.penalty_payment_status) AS latest_payment_status,
               (SELECT pp.provider_payment_id FROM penalty_payments pp WHERE pp.case_id=oc.id AND pp.status='verified' ORDER BY pp.id DESC LIMIT 1) AS penalty_payment_reference
        FROM overstay_cases oc JOIN bookings b ON b.id=oc.booking_id
        JOIN destinations d ON d.id=b.destination_id JOIN parking_levels pl ON pl.id=b.level_id
        JOIN parking_slots ps ON ps.id=b.slot_id WHERE {where_sql} ORDER BY b.expiry_time DESC
    """, params).fetchall()
    result = []
    current = datetime.now(timezone.utc)
    for row in rows:
        item = dict(row)
        if item["status"] in ("awaiting_departure_confirmation", "overstay"):
            minutes = billable_overstay_minutes(parse_booking_timestamp(item["overstay_started_at"]), current)
            item["current_overstay_minutes"] = minutes
            item["estimated_penalty"] = minutes * item["rate_per_minute"]
        else:
            item["current_overstay_minutes"] = item["duration_minutes"] or 0
        item["penalty_payment_status"] = item.pop("latest_payment_status")
        if item["penalty_payment_status"] == "verified":
            item["penalty_payment_status"] = "paid"
        item["detection_history"] = [dict(event) for event in db().execute("""
            SELECT e.detected_status,e.detected_at,e.prediction_id FROM overstay_detection_events e
            WHERE e.case_id=? ORDER BY e.detected_at
        """, (item["id"],)).fetchall()]
        result.append(item)
    return result

@APP.get("/api/overstays")
@require_role("customer")
def customer_overstays():
    reconcile_overstays(db()); db().commit()
    return jsonify(overstays=overstay_rows("b.customer_id=?", (g.user["id"],)))

@APP.get("/api/manager/overstays")
@require_role("manager")
def manager_overstays():
    reconcile_overstays(db()); db().commit()
    return jsonify(overstays=overstay_rows("b.destination_id=?", (g.user["destination_id"],)))

@APP.post("/api/bookings/hold")
@require_role("customer")
def create_booking_hold():
    data = request.get_json(silent=True) or {}
    vehicle_type = str(data.get("vehicle_type", "")).strip()
    level_id = data.get("level_id")
    slot_id = data.get("slot_id")
    try:
        level_id = int(level_id)
        slot_id = int(slot_id)
    except (TypeError, ValueError):
        return jsonify(error="Choose a valid parking level, slot, and duration."), 400
    if vehicle_type not in {"car", "bike", "ev_car"}:
        return jsonify(error="Choose Car, Bike, or EV Car."), 400
    try:
        start = parse_booking_timestamp(data.get("start_time", ""))
        expiry = parse_booking_timestamp(data.get("expiry_time", ""))
    except (TypeError, ValueError, OverflowError):
        return jsonify(error="Choose a valid start and end date and time."), 400
    current_time = datetime.now(timezone.utc)
    duration = int((expiry - start).total_seconds() // 60)
    if start < current_time:
        return jsonify(error="The reservation start time cannot be in the past."), 400
    if start.minute % 30 or start.second or start.microsecond:
        return jsonify(error="Start times must align to a 30-minute boundary."), 400
    if expiry <= start or duration < 30 or (expiry - start).total_seconds() % 1800:
        return jsonify(error="Choose an end time at least 30 minutes after the start time."), 400
    if duration > MAX_BOOKING_HOURS * 60:
        return jsonify(error=f"Bookings can be up to {MAX_BOOKING_HOURS} hours long."), 400
    if expiry > current_time + timedelta(days=MAX_BOOKING_DAYS):
        return jsonify(error=f"Bookings can be made up to {MAX_BOOKING_DAYS} days ahead."), 400
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cleanup_expired_holds(conn)
        current_time = datetime.now(timezone.utc)
        if start < current_time:
            conn.rollback()
            return jsonify(error="The reservation start time has passed. Choose a new start time."), 400
        slot = conn.execute("""
            SELECT parking_slots.*, parking_levels.destination_id
            FROM parking_slots JOIN parking_levels ON parking_levels.id=parking_slots.level_id
            WHERE parking_slots.id=? AND parking_slots.level_id=?
        """, (slot_id, level_id)).fetchone()
        if not slot:
            conn.rollback()
            return jsonify(error="That parking slot does not belong to the selected level."), 400
        compatible = set((slot["compatible_vehicle_types"] or "").split(","))
        if vehicle_type not in compatible:
            conn.rollback()
            return jsonify(error="This slot is not compatible with the selected vehicle type."), 409
        if slot["status"] != "available" or not slot["updated_at"]:
            conn.rollback()
            return jsonify(error="This slot does not have a recent confirmed available status."), 409
        try:
            occupancy_updated = parse_booking_timestamp(slot["updated_at"])
        except (TypeError, ValueError):
            occupancy_updated = datetime.min.replace(tzinfo=timezone.utc)
        if (current_time - occupancy_updated) > timedelta(minutes=OCCUPANCY_MAX_AGE_MINUTES):
            conn.rollback()
            return jsonify(error="This slot's availability is outdated. Please choose a slot with a recent update."), 409
        conflict = conn.execute("""
            SELECT 1 FROM bookings
            WHERE slot_id=? AND (status IN ('confirmed','active') OR (status='payment_pending' AND hold_expires_at>?))
              AND start_time < ? AND expiry_time > ?
            UNION ALL
            SELECT 1 FROM overstay_cases
            WHERE slot_id=? AND status IN ('awaiting_departure_confirmation','overstay')
        """, (slot_id, iso_time(current_time), iso_time(expiry), iso_time(start), slot_id)).fetchone()
        if conflict:
            conn.rollback()
            return jsonify(error="That slot is no longer available for the selected time window."), 409
        pricing = conn.execute(
            "SELECT rate_per_block,currency FROM location_pricing WHERE destination_id=? AND active=1",
            (slot["destination_id"],),
        ).fetchone()
        if not pricing:
            conn.rollback()
            return jsonify(error="Pricing is not configured for this location."), 503
        blocks = (duration + 29) // 30
        loyalty = loyalty_summary(conn, g.user["id"])
        discount_percent = loyalty["discount_percent"]
        quote = discounted_booking_quote(pricing["rate_per_block"], discount_percent, blocks)
        rate_per_block = quote["discounted_rate_paise"] / 100
        total = quote["total_amount_paise"] / 100
        created = now()
        hold_expires = iso_time(datetime.now(timezone.utc) + timedelta(minutes=PAYMENT_HOLD_MINUTES))
        booking_code = f"PZ-{secrets.token_hex(5).upper()}"
        cur = conn.execute("""
            INSERT INTO bookings(booking_code,customer_id,destination_id,level_id,slot_id,vehicle_type,start_time,expiry_time,duration_minutes,rate_per_block,blocks,total_amount,loyalty_tier,base_rate_per_block,discount_percent,discount_amount_paise,rate_per_block_paise,total_amount_paise,currency,status,hold_expires_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (booking_code, g.user["id"], slot["destination_id"], level_id, slot_id, vehicle_type, iso_time(start), iso_time(expiry), duration, rate_per_block, blocks, total, loyalty["current_tier"], pricing["rate_per_block"], discount_percent, quote["discount_amount_paise"], quote["discounted_rate_paise"], quote["total_amount_paise"], pricing["currency"], "payment_pending", hold_expires, created))
        booking_id = cur.lastrowid
        conn.execute("INSERT INTO slot_holds(slot_id,booking_id,start_time,expiry_time,hold_expires_at,status) VALUES(?,?,?,?,?,?)", (slot_id, booking_id, iso_time(start), iso_time(expiry), hold_expires, "active"))
        order, order_error = razorpay_order(total, booking_code, quote["total_amount_paise"])
        if order_error:
            conn.rollback()
            return jsonify(error=order_error), 503
        if int(order.get("amount", -1)) != quote["total_amount_paise"] or order.get("currency") != pricing["currency"]:
            conn.rollback()
            return jsonify(error="Payment order amount did not match the saved booking quote. Please retry."), 502
        conn.execute("INSERT INTO payments(booking_id,provider,provider_order_id,amount,amount_paise,currency,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (booking_id, "razorpay", order["id"], total, quote["total_amount_paise"], pricing["currency"], "created", created))
        conn.commit()
        return jsonify(
            booking_id=booking_id,
            booking_code=booking_code,
            start_time=iso_time(start),
            expiry_time=iso_time(expiry),
            duration_minutes=duration,
            blocks=blocks,
            loyalty_tier=loyalty["current_tier"],
            base_rate_per_block=pricing["rate_per_block"],
            discount_percent=discount_percent,
            discount_amount=quote["discount_amount_paise"] / 100,
            discount_amount_paise=quote["discount_amount_paise"],
            rate_per_block=rate_per_block,
            rate_per_block_paise=quote["discounted_rate_paise"],
            total_amount=total,
            total_amount_paise=quote["total_amount_paise"],
            currency=pricing["currency"],
            hold_expires_at=hold_expires,
            razorpay={"key_id": RAZORPAY_KEY_ID, "order_id": order["id"], "amount": order["amount"], "currency": order["currency"], "name": "ParkZenOne", "description": f"Parking reservation {booking_code}"},
        ), 201
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify(error="That slot was just reserved by another customer. Please choose another slot."), 409
    except Exception:
        conn.rollback()
        APP.logger.exception("Booking hold failed")
        return jsonify(error="Unable to hold that parking slot right now."), 500

@APP.post("/api/bookings/<int:booking_id>/verify-payment")
@require_role("customer")
def verify_booking_payment(booking_id):
    data = request.get_json(silent=True) or {}
    conn = db()
    row = conn.execute("""
        SELECT bookings.*, payments.provider_order_id, payments.amount_paise AS payment_amount_paise, payments.status AS payment_status
        FROM bookings JOIN payments ON payments.booking_id=bookings.id
        WHERE bookings.id=? AND bookings.customer_id=?
    """, (booking_id, g.user["id"])).fetchone()
    if not row:
        return jsonify(error="Booking not found."), 404
    if row["status"] == "confirmed":
        return jsonify(message="Booking already confirmed.", booking_code=row["booking_code"])
    if row["payment_amount_paise"] != row["total_amount_paise"]:
        return jsonify(error="The saved payment amount does not match this booking quote."), 409
    if row["status"] != "payment_pending" or not row["hold_expires_at"] or row["hold_expires_at"] <= now():
        return jsonify(error="This payment hold has expired. Please create a new booking."), 409
    payment_id = str(data.get("razorpay_payment_id", ""))
    signature = str(data.get("razorpay_signature", ""))
    if not payment_id or not signature or not RAZORPAY_KEY_SECRET:
        return jsonify(error="Payment verification data is incomplete or Razorpay is not configured."), 400
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f"{row['provider_order_id']}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify(error="Payment signature verification failed. The booking was not confirmed."), 400
    payment_details, payment_error = razorpay_payment(payment_id)
    if payment_error or not payment_details:
        return jsonify(error="Unable to verify the payment with Razorpay. The booking remains pending."), 503
    if (payment_details.get("order_id") != row["provider_order_id"] or
            payment_details.get("amount") != row["total_amount_paise"] or
            payment_details.get("currency") != row["currency"] or
            payment_details.get("status") != "captured"):
        return jsonify(error="The verified payment does not match this booking amount and order."), 400
    try:
        conn.execute("BEGIN IMMEDIATE")
        cleanup_expired_holds(conn)
        current = conn.execute("SELECT * FROM bookings WHERE id=? AND customer_id=? AND status='payment_pending'", (booking_id, g.user["id"])).fetchone()
        if not current or current["hold_expires_at"] <= now():
            conn.rollback()
            return jsonify(error="This payment hold expired before verification. Do not use this payment for a new booking."), 409
        conflict = conn.execute("""
            SELECT 1 FROM bookings
            WHERE slot_id=? AND id<>? AND status IN ('confirmed','active')
              AND start_time < ? AND expiry_time > ?
            UNION ALL
            SELECT 1 FROM overstay_cases
            WHERE slot_id=? AND status IN ('awaiting_departure_confirmation','overstay')
        """, (current["slot_id"], booking_id, current["expiry_time"], current["start_time"], current["slot_id"])).fetchone()
        if conflict:
            conn.rollback()
            return jsonify(error="The slot became unavailable before payment verification. Contact support with your payment reference."), 409
        conn.execute("UPDATE bookings SET status='confirmed',hold_expires_at=NULL WHERE id=? AND customer_id=? AND status='payment_pending'", (booking_id, g.user["id"]))
        conn.execute("UPDATE slot_holds SET status='confirmed' WHERE booking_id=?", (booking_id,))
        conn.execute("UPDATE payments SET provider_payment_id=?,method=?,status='verified',verified_at=? WHERE booking_id=?", (payment_id, str(data.get("method", "razorpay")), now(), booking_id))
        add_notification(conn, g.user["id"], "Booking confirmed",
            f"Your booking {row['booking_code']} is confirmed after successful payment.",
            "booking_confirmed", f"booking:{booking_id}:confirmed", booking_id, f"booking:{booking_id}")
        add_notification(conn, g.user["id"], "Payment successful",
            f"Payment for booking {row['booking_code']} was verified successfully.",
            "payment_success", f"booking:{booking_id}:payment-verified", booking_id, f"booking:{booking_id}")
        conn.commit()
    except Exception:
        conn.rollback()
        APP.logger.exception("Booking payment verification failed")
        return jsonify(error="Unable to confirm the booking safely. No booking confirmation was issued."), 500
    return jsonify(message="Payment verified and booking confirmed.", booking_code=row["booking_code"])

@APP.post("/api/overstays/<int:case_id>/payment-order")
@require_role("customer")
def create_penalty_payment_order(case_id):
    conn = db()
    case = conn.execute("""
        SELECT oc.*,b.customer_id,b.currency,b.booking_code FROM overstay_cases oc
        JOIN bookings b ON b.id=oc.booking_id WHERE oc.id=? AND b.customer_id=?
    """, (case_id, g.user["id"])).fetchone()
    if not case:
        return jsonify(error="Overstay record not found."), 404
    if case["final_penalty"] is None or not case["departure_detected_at"]:
        return jsonify(error="The penalty is still an estimate. Payment is available after departure is confirmed."), 409
    if case["final_penalty"] <= 0:
        return jsonify(error="There is no penalty due for this booking."), 409
    if case["penalty_payment_status"] == "paid":
        return jsonify(error="This penalty has already been paid."), 409
    pending = conn.execute("SELECT * FROM penalty_payments WHERE case_id=? AND status IN ('created','pending') ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if pending and pending["provider_order_id"]:
        return jsonify(payment_id=pending["id"],amount=pending["amount"],currency=pending["currency"],
                       razorpay={"key_id":RAZORPAY_KEY_ID,"order_id":pending["provider_order_id"],"amount":pending["amount"]*100,"currency":pending["currency"],"name":"ParkZenOne","description":f"Overstay penalty {case['booking_code']}"})
    order, error = razorpay_order(case["final_penalty"], f"PEN-{case['booking_code']}-{secrets.token_hex(3).upper()}")
    if error:
        return jsonify(error=error), 503
    created = now()
    cur = conn.execute("""
        INSERT INTO penalty_payments(case_id,provider,provider_order_id,amount,currency,status,created_at)
        VALUES(?,?,?,?,?,'created',?)
    """, (case_id,"razorpay",order["id"],case["final_penalty"],case["currency"],created))
    conn.execute("UPDATE overstay_cases SET penalty_payment_status='pending',status='penalty_pending',updated_at=? WHERE id=?", (created,case_id))
    add_notification(conn, case["customer_id"], "Penalty payment is available",
        f"The confirmed overstay penalty for booking {case['booking_code']} is ready for payment.",
        "penalty_due", f"overstay:{case_id}:penalty-due", case["booking_id"], f"overstay:{case_id}")
    add_notification(conn, case["customer_id"], "Overstay status updated",
        f"Booking {case['booking_code']} overstay status is now penalty pending.",
        "overstay_update", f"overstay:{case_id}:status:penalty_pending", case["booking_id"], f"overstay:{case_id}")
    conn.commit()
    return jsonify(payment_id=cur.lastrowid,amount=case["final_penalty"],currency=case["currency"],
                   razorpay={"key_id":RAZORPAY_KEY_ID,"order_id":order["id"],"amount":order["amount"],"currency":order["currency"],"name":"ParkZenOne","description":f"Overstay penalty {case['booking_code']}"}),201

@APP.post("/api/overstays/<int:case_id>/verify-payment")
@require_role("customer")
def verify_penalty_payment(case_id):
    data = request.get_json(silent=True) or {}
    conn = db()
    row = conn.execute("""
        SELECT pp.*,oc.final_penalty,b.customer_id FROM penalty_payments pp
        JOIN overstay_cases oc ON oc.id=pp.case_id JOIN bookings b ON b.id=oc.booking_id
        WHERE oc.id=? AND b.customer_id=? AND pp.status IN ('created','pending','verified')
        ORDER BY pp.id DESC LIMIT 1
    """, (case_id,g.user["id"])).fetchone()
    if not row:
        return jsonify(error="Penalty payment order not found."),404
    if row["status"]=="verified":
        return jsonify(message="Penalty payment already verified."),200
    payment_id=str(data.get("razorpay_payment_id", "")); signature=str(data.get("razorpay_signature", ""))
    if not payment_id or not signature or not RAZORPAY_KEY_SECRET:
        return jsonify(error="Payment verification data is incomplete or Razorpay is not configured."),400
    expected=hmac.new(RAZORPAY_KEY_SECRET.encode(),f"{row['provider_order_id']}|{payment_id}".encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):
        return jsonify(error="Penalty payment signature verification failed."),400
    details,error=razorpay_payment(payment_id)
    if error or not details:
        return jsonify(error="Unable to verify this penalty payment with Razorpay."),503
    if details.get("order_id")!=row["provider_order_id"] or details.get("amount")!=row["amount"]*100 or details.get("currency")!=row["currency"]:
        return jsonify(error="The verified payment does not match this penalty amount and order."),400
    if details.get("status") == "failed":
        stamp = now()
        conn.execute("UPDATE penalty_payments SET provider_payment_id=?,method=?,status='failed',verified_at=? WHERE id=?", (payment_id,str(data.get("method","razorpay")),stamp,row["id"]))
        conn.execute("UPDATE overstay_cases SET penalty_payment_status='failed',status='completed',updated_at=? WHERE id=?", (stamp,case_id))
        booking = conn.execute("SELECT b.id,b.customer_id,b.booking_code FROM bookings b JOIN overstay_cases oc ON oc.booking_id=b.id WHERE oc.id=?", (case_id,)).fetchone()
        add_notification(conn, booking["customer_id"], "Penalty payment failed",
            f"Razorpay could not verify payment for the overstay penalty on booking {booking['booking_code']}. The penalty remains unpaid.",
            "penalty_payment_failed", f"overstay:{case_id}:payment-failed:{row['id']}", booking["id"], f"overstay:{case_id}")
        conn.commit()
        return jsonify(error="Razorpay reports that penalty payment failed. No penalty was marked paid."), 402
    if details.get("status") != "captured":
        conn.execute("UPDATE penalty_payments SET provider_payment_id=?,method=?,status='pending' WHERE id=?", (payment_id,str(data.get("method","razorpay")),row["id"]))
        conn.commit()
        return jsonify(error="Razorpay has not captured this penalty payment yet. Its status remains pending."), 409
    stamp=now()
    conn.execute("UPDATE penalty_payments SET provider_payment_id=?,method=?,status='verified',verified_at=? WHERE id=? AND status IN ('created','pending')",(payment_id,str(data.get("method","razorpay")),stamp,row["id"]))
    conn.execute("UPDATE overstay_cases SET penalty_payment_status='paid',status='penalty_paid',updated_at=? WHERE id=? AND final_penalty=?",(stamp,case_id,row["final_penalty"]))
    booking = conn.execute("SELECT b.id,b.customer_id,b.booking_code FROM bookings b JOIN overstay_cases oc ON oc.booking_id=b.id WHERE oc.id=?", (case_id,)).fetchone()
    add_notification(conn, booking["customer_id"], "Penalty payment successful",
        f"Your overstay penalty for booking {booking['booking_code']} was verified as paid.",
        "penalty_paid", f"overstay:{case_id}:penalty-paid", booking["id"], f"overstay:{case_id}")
    conn.commit()
    return jsonify(message="Penalty payment verified.",booking_code="",payment_reference=payment_id)

@APP.patch("/api/profile")
@require_role()
def update_profile():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip(); mobile = str(data.get("mobile", "")).strip()
    if not name or len(mobile) < 7: return jsonify(error="Enter a name and valid mobile number."), 400
    db().execute("UPDATE users SET name=?,mobile=? WHERE id=?", (name, mobile, g.user["id"])); db().commit()
    return jsonify(user=user_json(db().execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()))

def customer_protection(conn, user_id):
    stamp = now()
    active_bookings = conn.execute("""
        SELECT COUNT(*) FROM bookings WHERE customer_id=? AND expiry_time>?
          AND (status IN ('confirmed','active') OR (status='payment_pending' AND hold_expires_at>?))
    """, (user_id, stamp, stamp)).fetchone()[0]
    open_penalties = conn.execute("""
        SELECT COUNT(*) FROM overstay_cases oc JOIN bookings b ON b.id=oc.booking_id
        WHERE b.customer_id=? AND (oc.status IN ('awaiting_departure_confirmation','overstay','penalty_pending')
          OR (oc.final_penalty>0 AND oc.penalty_payment_status!='paid'))
    """, (user_id,)).fetchone()[0]
    booking_records = conn.execute("SELECT COUNT(*) FROM bookings WHERE customer_id=?", (user_id,)).fetchone()[0]
    activity_records = conn.execute("SELECT COUNT(*) FROM activity WHERE user_id=?", (user_id,)).fetchone()[0]
    activity_records += conn.execute("SELECT COUNT(*) FROM loyalty_activity_days WHERE user_id=?", (user_id,)).fetchone()[0]
    warnings = []
    if active_bookings:
        warnings.append(f"{active_bookings} active or upcoming booking(s)")
    if open_penalties:
        warnings.append(f"{open_penalties} unresolved overstay penalty/case(s)")
    if booking_records or activity_records:
        warnings.append("transaction or activity history must be retained")
    return {
        "active_bookings": active_bookings,
        "unresolved_overstays": open_penalties,
        "can_deactivate": active_bookings == 0 and open_penalties == 0,
        "can_delete": booking_records == 0 and activity_records == 0 and open_penalties == 0,
        "warnings": warnings,
    }

@APP.get("/api/admin/capacity")
@require_role("admin")
def admin_capacity():
    conn = db()
    check_capacity_alerts(conn)
    count = active_customer_count(conn)
    events = conn.execute("""
        SELECT threshold,customer_count,account_limit,message,email_status,created_at
        FROM capacity_alert_events ORDER BY id DESC LIMIT 20
    """).fetchall()
    current_alerts = conn.execute("""
        SELECT e.threshold,e.customer_count,e.account_limit,e.message,e.email_status,e.created_at
        FROM capacity_alert_states s JOIN capacity_alert_events e ON e.threshold=s.threshold
        WHERE s.active=1 AND e.id=(SELECT MAX(id) FROM capacity_alert_events WHERE threshold=s.threshold)
        ORDER BY e.threshold DESC
    """).fetchall()
    return jsonify(
        customer_count=count, account_limit=MAX_CUSTOMER_ACCOUNTS,
        remaining=max(0, MAX_CUSTOMER_ACCOUNTS-count),
        percentage=round(min(100, count*100/MAX_CUSTOMER_ACCOUNTS), 1),
        alerts=[dict(row) for row in current_alerts], recent_alerts=[dict(row) for row in events],
        email_notifications_configured=EMAIL_ALERTS_CONFIGURED,
        inactivity_default_days=DEFAULT_INACTIVITY_DAYS,
    )

@APP.get("/api/admin/customers/inactive")
@require_role("admin")
def admin_inactive_customers():
    try:
        days = int(request.args.get("days", DEFAULT_INACTIVITY_DAYS))
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(100, max(5, int(request.args.get("page_size", 20))))
    except (TypeError, ValueError):
        return jsonify(error="Choose valid inactivity and page values."), 400
    if days < 1 or days > 3650:
        return jsonify(error="Inactivity period must be between 1 and 3650 days."), 400
    search = str(request.args.get("search", "")).strip()[:120]
    cutoff = iso_time(datetime.now(timezone.utc)-timedelta(days=days))
    clause = "role='customer' AND COALESCE(last_active_at,created_at)<=?"
    params = [cutoff]
    if search:
        clause += " AND (name LIKE ? OR email LIKE ? OR mobile LIKE ?)"
        params.extend([f"%{search}%"]*3)
    conn = db()
    total = conn.execute(f"SELECT COUNT(*) FROM users WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(f"""
        SELECT id,name,email,mobile,created_at,last_active_at,is_active FROM users
        WHERE {clause} ORDER BY is_active DESC,COALESCE(last_active_at,created_at),id
        LIMIT ? OFFSET ?
    """, [*params,page_size,(page-1)*page_size]).fetchall()
    customers = []
    for row in rows:
        item = dict(row)
        item["status"] = "active" if item["is_active"] else "deactivated"
        item.update(customer_protection(conn, item["id"]))
        customers.append(item)
    return jsonify(customers=customers,total=total,page=page,page_size=page_size,
                   pages=max(1,(total+page_size-1)//page_size),inactivity_days=days,cutoff=cutoff)

@APP.post("/api/admin/customers/deactivate")
@require_role("admin")
def admin_deactivate_customers():
    data = request.get_json(silent=True) or {}
    ids = data.get("user_ids")
    if data.get("confirmed") is not True or not isinstance(ids,list) or not ids:
        return jsonify(error="Select customer accounts and confirm deactivation."),400
    try:
        ids = sorted(set(int(value) for value in ids))
    except (TypeError,ValueError):
        return jsonify(error="The selected customer list is invalid."),400
    conn=db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows=conn.execute(f"SELECT id,email,is_active FROM users WHERE role='customer' AND id IN ({','.join('?' for _ in ids)})",ids).fetchall()
        if len(rows)!=len(ids):
            conn.rollback();return jsonify(error="One or more selected accounts were not found or are not customer accounts."),404
        blocked=[]
        for row in rows:
            protection=customer_protection(conn,row["id"])
            if row["is_active"] and not protection["can_deactivate"]:
                blocked.append({"email":row["email"],"warnings":protection["warnings"]})
        if blocked:
            conn.rollback();return jsonify(error="Some selected customers have active bookings or unresolved penalties and cannot be deactivated.",blocked=blocked),409
        stamp=now()
        for row in rows:
            if not row["is_active"]: continue
            conn.execute("UPDATE users SET is_active=0 WHERE id=? AND role='customer'",(row["id"],))
            conn.execute("DELETE FROM sessions WHERE user_id=?",(row["id"],))
            conn.execute("INSERT INTO admin_audit_log(actor_admin_id,affected_user_id,affected_email,action,details,created_at) VALUES(?,?,?,?,?,?)",(g.user["id"],row["id"],row["email"],"deactivate","Inactive customer account deactivated by administrator",stamp))
        conn.commit();check_capacity_alerts(conn)
    except Exception:
        conn.rollback();APP.logger.exception("Customer deactivation failed")
        return jsonify(error="Unable to deactivate the selected customer accounts."),500
    return jsonify(message=f"Deactivated {len(rows)} customer account(s).")

@APP.post("/api/admin/customers/<int:user_id>/reactivate")
@require_role("admin")
def admin_reactivate_customer(user_id):
    conn=db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user=conn.execute("SELECT id,email,is_active FROM users WHERE id=? AND role='customer'",(user_id,)).fetchone()
        if not user:
            conn.rollback();return jsonify(error="Customer account not found."),404
        if user["is_active"]:
            conn.rollback();return jsonify(error="This customer account is already active."),409
        count=active_customer_count(conn)
        if count>=MAX_CUSTOMER_ACCOUNTS:
            conn.rollback();return jsonify(error=f"Customer capacity is full ({MAX_CUSTOMER_ACCOUNTS}). Deactivate an account before reactivating this customer."),409
        stamp=now()
        conn.execute("UPDATE users SET is_active=1 WHERE id=? AND role='customer'",(user_id,))
        conn.execute("INSERT INTO admin_audit_log(actor_admin_id,affected_user_id,affected_email,action,details,created_at) VALUES(?,?,?,?,?,?)",(g.user["id"],user_id,user["email"],"reactivate","Customer account reactivated by administrator",stamp))
        conn.commit();check_capacity_alerts(conn)
    except Exception:
        conn.rollback();APP.logger.exception("Customer reactivation failed")
        return jsonify(error="Unable to reactivate this customer account."),500
    return jsonify(message="Customer account reactivated.")

@APP.delete("/api/admin/customers/<int:user_id>")
@require_role("admin")
def admin_delete_customer(user_id):
    data=request.get_json(silent=True) or {}
    conn=db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user=conn.execute("SELECT id,email,is_active FROM users WHERE id=? AND role='customer'",(user_id,)).fetchone()
        if not user:
            conn.rollback();return jsonify(error="Customer account not found."),404
        if str(data.get("confirm_email","")).strip().lower()!=user["email"].lower():
            conn.rollback();return jsonify(error="Enter the customer's exact email to confirm deletion."),400
        protection=customer_protection(conn,user_id)
        if not protection["can_delete"]:
            conn.rollback();return jsonify(error="This account has records that must be retained. Deactivate it instead.",warnings=protection["warnings"]),409
        stamp=now()
        conn.execute("DELETE FROM sessions WHERE user_id=?",(user_id,))
        conn.execute("INSERT INTO admin_audit_log(actor_admin_id,affected_user_id,affected_email,action,details,created_at) VALUES(?,?,?,?,?,?)",(g.user["id"],user_id,user["email"],"delete","Customer account deleted after exact-email confirmation; no retained records existed",stamp))
        conn.execute("DELETE FROM users WHERE id=? AND role='customer'",(user_id,))
        conn.commit()
    except Exception:
        conn.rollback();APP.logger.exception("Customer deletion failed")
        return jsonify(error="Unable to delete this customer account."),500
    return jsonify(message="Customer account deleted.")

@APP.get("/api/admin/audit")
@require_role("admin")
def admin_cleanup_audit():
    rows=db().execute("""
        SELECT l.id,l.affected_user_id,l.affected_email,l.action,l.details,l.created_at,u.name AS actor_name
        FROM admin_audit_log l JOIN users u ON u.id=l.actor_admin_id ORDER BY l.id DESC LIMIT 100
    """).fetchall()
    return jsonify(actions=[dict(row) for row in rows])

@APP.get("/api/admin/locations")
@require_role("admin")
def admin_locations():
    rows = db().execute("""
        SELECT destinations.id, destinations.name, destinations.type, destinations.address,
               users.id AS manager_id, users.name AS manager_name, users.email AS manager_email
        FROM destinations
        LEFT JOIN users ON users.destination_id=destinations.id AND users.role='manager'
        ORDER BY destinations.name
    """).fetchall()
    return jsonify(locations=[dict(row) for row in rows])

@APP.post("/api/admin/managers")
@require_role("admin")
def create_manager():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    mobile = str(data.get("mobile", "")).strip()
    password = str(data.get("password", ""))
    destination_id = str(data.get("destination_id", "")).strip()
    if not name or "@" not in email or len(password) < 8 or not destination_id:
        return jsonify(error="Enter a name, valid email, password of at least 8 characters, and location."), 400
    if not db().execute("SELECT 1 FROM destinations WHERE id=?", (destination_id,)).fetchone():
        return jsonify(error="Choose a valid parking location."), 400
    try:
        cur = db().execute(
            "INSERT INTO users(name,email,mobile,password,role,destination_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (name, email, mobile, generate_password_hash(password), "manager", destination_id, now()),
        )
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="That email or location is already assigned."), 409
    return jsonify(user=user_json(db().execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())), 201

@APP.patch("/api/admin/locations/<destination_id>/manager")
@require_role("admin")
def assign_manager(destination_id):
    data = request.get_json(silent=True) or {}
    manager_id = data.get("manager_id")
    if not db().execute("SELECT 1 FROM destinations WHERE id=?", (destination_id,)).fetchone():
        return jsonify(error="Location not found."), 404
    if manager_id in (None, ""):
        db().execute("UPDATE users SET destination_id=NULL WHERE role='manager' AND destination_id=?", (destination_id,))
    else:
        try:
            manager_id = int(manager_id)
        except (TypeError, ValueError):
            return jsonify(error="Choose a valid manager account."), 400
        manager = db().execute("SELECT id FROM users WHERE id=? AND role='manager'", (manager_id,)).fetchone()
        if not manager:
            return jsonify(error="Choose a valid manager account."), 400
        # Clear both sides first so the partial unique index permits a replacement.
        db().execute("UPDATE users SET destination_id=NULL WHERE role='manager' AND destination_id=?", (destination_id,))
        db().execute("UPDATE users SET destination_id=NULL WHERE role='manager' AND id=?", (manager_id,))
        db().execute("UPDATE users SET destination_id=? WHERE id=? AND role='manager'", (destination_id, manager_id))
    db().commit()
    return jsonify(message="Manager assignment updated.")

@APP.get("/api/admin/overview")
@require_role("admin")
def admin_overview():
    conn = db()
    check_capacity_alerts(conn)
    count=active_customer_count(conn)
    alerts=conn.execute("""
        SELECT e.threshold,e.customer_count,e.account_limit,e.message,e.email_status,e.created_at
        FROM capacity_alert_states s JOIN capacity_alert_events e ON e.threshold=s.threshold
        WHERE s.active=1 AND e.id=(SELECT MAX(id) FROM capacity_alert_events WHERE threshold=s.threshold)
        ORDER BY e.threshold DESC
    """).fetchall()
    recent=conn.execute("SELECT threshold,customer_count,account_limit,message,email_status,created_at FROM capacity_alert_events ORDER BY id DESC LIMIT 5").fetchall()
    return jsonify(
        customers=count,managers=conn.execute("SELECT COUNT(*) FROM users WHERE role='manager' AND is_active=1").fetchone()[0],
        destinations=conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0],
        confirmed_updates=conn.execute("SELECT COUNT(*) FROM predictions WHERE status='confirmed'").fetchone()[0],
        capacity={"customer_count":count,"account_limit":MAX_CUSTOMER_ACCOUNTS,
                  "remaining":max(0,MAX_CUSTOMER_ACCOUNTS-count),
                  "percentage":round(min(100,count*100/MAX_CUSTOMER_ACCOUNTS),1),
                  "email_notifications_configured":EMAIL_ALERTS_CONFIGURED,
                  "alerts":[dict(row) for row in alerts],"recent_alerts":[dict(row) for row in recent]},
        latest_updates=[dict(row) for row in conn.execute("SELECT destinations.name,availability.total,availability.available,availability.occupied,availability.updated_at FROM availability JOIN destinations ON destinations.id=availability.destination_id ORDER BY availability.updated_at DESC")]
    )

@APP.get("/api/admin/users")
@require_role("admin")
def admin_users():
    rows = db().execute("SELECT users.id,users.name,users.email,users.mobile,users.role,users.destination_id,destinations.name AS destination_name,users.created_at,users.is_active,users.last_active_at FROM users LEFT JOIN destinations ON destinations.id=users.destination_id ORDER BY users.role,users.name").fetchall()
    return jsonify(users=[dict(row) for row in rows])

initialize()

def overstay_monitor():
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                create_due_duration_reminders(conn)
                reconcile_overstays(conn)
                check_capacity_alerts(conn)
                dispatch_capacity_alert_emails(conn)
        except Exception:
            APP.logger.exception("Background overstay check failed")
        time.sleep(OVERSTAY_MONITOR_INTERVAL_SECONDS)

def start_overstay_monitor():
    worker = threading.Thread(target=overstay_monitor, name="parkzen-overstay-monitor", daemon=True)
    worker.start()

if __name__ == "__main__":
    if os.environ.get("PARKZEN_DEBUG") != "1" or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_overstay_monitor()
    APP.run(host="127.0.0.1", port=5000, debug=os.environ.get("PARKZEN_DEBUG") == "1")
