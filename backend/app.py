"""ParkZen API: manager-confirmed YOLO parking updates and customer availability."""
from datetime import datetime, timezone
from functools import wraps
import os
from pathlib import Path
import secrets
import sqlite3
from werkzeug.security import check_password_hash, generate_password_hash
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PARKZEN_DB", ROOT / "parkzen.sqlite3"))
UPLOAD_DIR = ROOT / "uploads"
MODEL_PATH = ROOT / "best.pt"
APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
CORS(APP, resources={r"/api/*": {"origins": os.environ.get("PARKZEN_ORIGIN", "http://localhost:5173")}})
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
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, mobile TEXT NOT NULL DEFAULT '', password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'customer', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS destinations(id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, address TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, parking_ref TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS availability(destination_id TEXT PRIMARY KEY REFERENCES destinations(id), total INTEGER NOT NULL, available INTEGER NOT NULL, occupied INTEGER NOT NULL, updated_at TEXT NOT NULL, manager_id INTEGER REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS predictions(id INTEGER PRIMARY KEY, destination_id TEXT NOT NULL, manager_id INTEGER NOT NULL, image_path TEXT NOT NULL, total INTEGER NOT NULL, available INTEGER NOT NULL, occupied INTEGER NOT NULL, slots TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, destination_id TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        for row in DESTINATIONS:
            conn.execute("INSERT OR IGNORE INTO destinations VALUES(?,?,?,?,?,?,?)", row)
        if not conn.execute("SELECT 1 FROM users WHERE role='manager'").fetchone():
            conn.execute("INSERT INTO users(name,email,mobile,password,role,created_at) VALUES(?,?,?,?,?,?)", ("Parking Manager", "manager@parkzen.local", "", generate_password_hash(os.environ.get("PARKZEN_MANAGER_PASSWORD", "1234")), "manager", now()))
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
    return db().execute("SELECT users.*,sessions.token FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token=?", (token,)).fetchone()

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
            return fn(*args, **kwargs)
        return wrapped
    return decorate

def user_json(user):
    return {"id": user["id"], "name": user["name"], "email": user["email"], "mobile": user["mobile"], "role": user["role"]}

def destination_json(row):
    item = dict(row)
    availability = db().execute("SELECT total,available,occupied,updated_at FROM availability WHERE destination_id=?", (item["id"],)).fetchone()
    item["availability"] = dict(availability) if availability else None
    item["status"] = "No recent update" if not availability else ("Parking full" if availability["available"] == 0 else "Limited parking" if availability["available"] / max(1, availability["total"]) <= 0.2 else "Parking available")
    return item

@APP.get("/api/health")
def health():
    return jsonify(status="ok", model="configured" if MODEL_PATH.exists() else "missing")

@APP.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    name, email, mobile, password = (str(data.get(key, "")).strip() for key in ("name", "email", "mobile", "password"))
    if not name or "@" not in email or len(mobile) < 7 or len(password) < 8:
        return jsonify(error="Enter your name, a valid email, mobile number, and a password of at least 8 characters."), 400
    try:
        cur = db().execute("INSERT INTO users(name,email,mobile,password,role,created_at) VALUES(?,?,?,?,?,?)", (name, email.lower(), mobile, generate_password_hash(password), "customer", now()))
        db().commit()
        return jsonify(message="Account created. Please sign in."), 201
    except sqlite3.IntegrityError:
        return jsonify(error="An account with this email already exists."), 409

@APP.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    user = db().execute("SELECT * FROM users WHERE email=?", (str(data.get("email", "")).strip().lower(),)).fetchone()
    if not user or not check_password_hash(user["password"], str(data.get("password", ""))):
        return jsonify(error="Email or password is incorrect."), 401
    if data.get("role") and data["role"] != user["role"]:
        return jsonify(error=f"This account does not have {data['role']} access."), 403
    token = secrets.token_urlsafe(32)
    db().execute("INSERT INTO sessions VALUES(?,?,?)", (token, user["id"], now()))
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
    term = f"%{request.args.get('q','').strip()}%"
    kind = request.args.get("type", "all")
    rows = db().execute("SELECT * FROM destinations WHERE (name LIKE ? OR address LIKE ?) AND (?='all' OR type=?) ORDER BY name", (term, term, kind, kind)).fetchall()
    return jsonify(destinations=[destination_json(row) for row in rows])

@APP.get("/api/destinations/<destination_id>")
def destination_detail(destination_id):
    row = db().execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
    if not row: return jsonify(error="Destination not found."), 404
    item = destination_json(row)
    user = current_user()
    if user and user["role"] == "customer":
        db().execute("INSERT INTO activity(user_id,destination_id,action,created_at) VALUES(?,?,?,?)", (user["id"], destination_id, "Viewed parking", now())); db().commit()
    return jsonify(destination=item)

@APP.get("/api/history")
@require_role("customer")
def customer_history():
    rows = db().execute("SELECT activity.created_at,activity.action,destinations.* FROM activity JOIN destinations ON destinations.id=activity.destination_id WHERE user_id=? ORDER BY activity.id DESC LIMIT 50", (g.user["id"],)).fetchall()
    return jsonify(history=[dict(row) for row in rows])

@APP.get("/api/manager/predictions")
@require_role("manager")
def prediction_history():
    rows = db().execute("SELECT predictions.*,destinations.name AS destination_name FROM predictions JOIN destinations ON destinations.id=predictions.destination_id WHERE status='confirmed' ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify(history=[dict(row) for row in rows])

@APP.post("/api/manager/analyze")
@require_role("manager")
def analyze():
    destination_id = request.form.get("destination_id", "")
    image = request.files.get("image")
    destination = db().execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
    if not destination: return jsonify(error="Choose a valid parking location."), 400
    if not image or not image.filename: return jsonify(error="Choose a parking image to analyze."), 400
    if Path(image.filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}: return jsonify(error="Upload a JPG, PNG, or WebP image."), 400
    path = UPLOAD_DIR / f"{secrets.token_hex(12)}{Path(image.filename).suffix.lower()}"
    image.save(path)
    try:
        detections = []
        for result in load_model()(str(path), conf=0.25):
            for box in result.boxes:
                class_id = int(box.cls[0])
                if class_id in (0, 1):
                    detections.append({"status": "available" if class_id == 0 else "occupied", "confidence": round(float(box.conf[0]), 4), "coordinates": [round(float(x), 2) for x in box.xyxy[0].tolist()]})
        detections.sort(key=lambda item: (item["coordinates"][1], item["coordinates"][0]))
        for index, slot in enumerate(detections, 1): slot["slot_id"] = f"P{index:02d}"
        available = sum(slot["status"] == "available" for slot in detections)
        occupied = sum(slot["status"] == "occupied" for slot in detections)
        cur = db().execute("INSERT INTO predictions(destination_id,manager_id,image_path,total,available,occupied,slots,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (destination_id, g.user["id"], path.name, len(detections), available, occupied, __import__('json').dumps(detections), "pending", now()))
        db().commit()
        return jsonify(prediction_id=cur.lastrowid, destination_id=destination_id, destination_name=destination["name"], total=len(detections), available=available, occupied=occupied, slots=detections), 201
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
    db().execute("INSERT INTO availability VALUES(?,?,?,?,?,?) ON CONFLICT(destination_id) DO UPDATE SET total=excluded.total,available=excluded.available,occupied=excluded.occupied,updated_at=excluded.updated_at,manager_id=excluded.manager_id", (row["destination_id"], row["total"], row["available"], row["occupied"], confirmed, g.user["id"]))
    db().commit()
    return jsonify(message="Parking availability updated.", destination_id=row["destination_id"], updated_at=confirmed)

@APP.get("/api/profile")
@require_role()
def profile():
    return jsonify(user=user_json(g.user))

@APP.patch("/api/profile")
@require_role()
def update_profile():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip(); mobile = str(data.get("mobile", "")).strip()
    if not name or len(mobile) < 7: return jsonify(error="Enter a name and valid mobile number."), 400
    db().execute("UPDATE users SET name=?,mobile=? WHERE id=?", (name, mobile, g.user["id"])); db().commit()
    return jsonify(user=user_json(db().execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()))

@APP.get("/api/admin/overview")
@require_role("admin")
def admin_overview():
    conn = db()
    return jsonify(customers=conn.execute("SELECT COUNT(*) FROM users WHERE role='customer'").fetchone()[0], managers=conn.execute("SELECT COUNT(*) FROM users WHERE role='manager'").fetchone()[0], destinations=conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0], confirmed_updates=conn.execute("SELECT COUNT(*) FROM predictions WHERE status='confirmed'").fetchone()[0], latest_updates=[dict(row) for row in conn.execute("SELECT destinations.name,availability.total,availability.available,availability.occupied,availability.updated_at FROM availability JOIN destinations ON destinations.id=availability.destination_id ORDER BY availability.updated_at DESC")])

@APP.get("/api/admin/users")
@require_role("admin")
def admin_users():
    rows = db().execute("SELECT id,name,email,mobile,role,created_at FROM users ORDER BY role,name").fetchall()
    return jsonify(users=[dict(row) for row in rows])

initialize()

if __name__ == "__main__":
    APP.run(host="127.0.0.1", port=5000, debug=os.environ.get("PARKZEN_DEBUG") == "1")
