"""Feature 6 and 7 integration tests. Run from the repository root: python -m unittest backend.test_feature6"""
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone, date
from unittest.mock import patch
import hmac
import hashlib
from pathlib import Path

_TEMP = tempfile.TemporaryDirectory(prefix="parkzen-feature6-", ignore_cleanup_errors=True)
os.environ["PARKZEN_DB"] = str(Path(_TEMP.name) / "feature6.sqlite3")
os.environ["PARKZEN_MAX_CUSTOMER_ACCOUNTS"] = "300"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as parkzen
from werkzeug.security import generate_password_hash


class Feature6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = parkzen.APP.test_client()
        with parkzen.APP.app_context():
            row = parkzen.db().execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
            cls.admin_id = row["id"]
            cls.admin_token = "feature6-admin-test-token"
            parkzen.db().execute("INSERT OR REPLACE INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (cls.admin_token, cls.admin_id, parkzen.now()))
            parkzen.db().commit()

    def setUp(self):
        with parkzen.APP.app_context():
            conn = parkzen.db()
            ids = [r[0] for r in conn.execute("SELECT id FROM users WHERE role='customer'").fetchall()]
            if ids:
                slots = ','.join('?' for _ in ids)
                booking_ids = [r[0] for r in conn.execute(f"SELECT id FROM bookings WHERE customer_id IN ({slots})", ids).fetchall()]
                if booking_ids:
                    bslots = ','.join('?' for _ in booking_ids)
                    case_ids = [r[0] for r in conn.execute(f"SELECT id FROM overstay_cases WHERE booking_id IN ({bslots})", booking_ids).fetchall()]
                    if case_ids:
                        cslots = ','.join('?' for _ in case_ids)
                        conn.execute(f"DELETE FROM penalty_payments WHERE case_id IN ({cslots})", case_ids)
                        conn.execute(f"DELETE FROM overstay_detection_events WHERE case_id IN ({cslots})", case_ids)
                        conn.execute(f"DELETE FROM overstay_cases WHERE id IN ({cslots})", case_ids)
                    for table in ("payments", "slot_holds"):
                        conn.execute(f"DELETE FROM {table} WHERE booking_id IN ({bslots})", booking_ids)
                    conn.execute(f"DELETE FROM bookings WHERE id IN ({bslots})", booking_ids)
                for table, column in (("sessions", "user_id"), ("activity", "user_id"), ("loyalty_activity_days", "user_id")):
                    conn.execute(f"DELETE FROM {table} WHERE {column} IN ({slots})", ids)
            conn.execute("DELETE FROM users WHERE role='customer'")
            conn.execute("DELETE FROM admin_audit_log")
            conn.execute("DELETE FROM capacity_alert_events")
            conn.execute("UPDATE capacity_alert_states SET active=0")
            conn.commit()
        parkzen.MAX_CUSTOMER_ACCOUNTS = 300

    def auth(self, token=None):
        return {"Authorization": f"Bearer {token or self.admin_token}"}

    def add_customer(self, suffix, *, active=1, last_active=None, created=None):
        created = created or parkzen.now()
        email = f"feature6-{suffix}@example.test"
        with parkzen.APP.app_context():
            cur = parkzen.db().execute(
                "INSERT INTO users(name,email,mobile,password,role,created_at,is_active,last_active_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"Customer {suffix}", email, "1234567890", generate_password_hash("TestPassword123"), "customer", created, active, last_active or created),
            )
            parkzen.db().commit()
            return cur.lastrowid, email

    def test_80_95_100_thresholds_and_rearm(self):
        parkzen.MAX_CUSTOMER_ACCOUNTS = 10
        for i in range(8): self.add_customer(f"threshold-{i}")
        with parkzen.APP.app_context(): parkzen.check_capacity_alerts(parkzen.db())
        for i in range(8, 10): self.add_customer(f"threshold-{i}")
        with parkzen.APP.app_context(): parkzen.check_capacity_alerts(parkzen.db())
        with parkzen.APP.app_context():
            events = parkzen.db().execute("SELECT threshold,customer_count FROM capacity_alert_events ORDER BY threshold").fetchall()
            self.assertEqual([(r["threshold"], r["customer_count"]) for r in events], [(80, 8), (95, 10), (100, 10)])
            parkzen.db().execute("UPDATE users SET is_active=0 WHERE email='feature6-threshold-9@example.test'")
            parkzen.db().commit(); parkzen.check_capacity_alerts(parkzen.db())
        self.add_customer("threshold-new")
        with parkzen.APP.app_context(): parkzen.check_capacity_alerts(parkzen.db())
        with parkzen.APP.app_context():
            self.assertEqual(parkzen.db().execute("SELECT COUNT(*) FROM capacity_alert_events WHERE threshold=100").fetchone()[0], 2)

    def test_concurrent_registration_cannot_exceed_capacity(self):
        parkzen.MAX_CUSTOMER_ACCOUNTS = 1
        barrier = threading.Barrier(2)
        results = []
        def register(index):
            client = parkzen.APP.test_client()
            barrier.wait()
            response = client.post("/api/auth/register", json={"name": f"Concurrent {index}", "email": f"race-{index}@example.test", "mobile": "1234567890", "password": "TestPassword123"})
            results.append(response.status_code)
        workers = [threading.Thread(target=register, args=(i,)) for i in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(15)
        self.assertEqual(sorted(results), [201, 409])
        with parkzen.APP.app_context(): self.assertEqual(parkzen.active_customer_count(parkzen.db()), 1)

    def test_admin_authorization_and_inactive_filter(self):
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec="seconds")
        self.add_customer("old", last_active=old, created=old)
        self.add_customer("recent")
        self.assertEqual(self.client.get("/api/admin/capacity").status_code, 401)
        response = self.client.get("/api/admin/customers/inactive?days=90&search=old", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["email"] for row in response.json["customers"]], ["feature6-old@example.test"])
        self.assertNotIn("password", response.json["customers"][0])

    def test_active_booking_protects_account_from_deactivation(self):
        user_id, _ = self.add_customer("protected")
        with parkzen.APP.app_context():
            conn = parkzen.db()
            level = conn.execute("SELECT id FROM parking_levels LIMIT 1").fetchone()[0]
            slot = conn.execute("SELECT id FROM parking_slots WHERE level_id=? LIMIT 1", (level,)).fetchone()[0]
            conn.execute("INSERT INTO bookings(booking_code,customer_id,destination_id,level_id,slot_id,vehicle_type,start_time,expiry_time,duration_minutes,rate_per_block,blocks,total_amount,currency,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("TEST-PROTECTED", user_id, "mall-a", level, slot, "car", parkzen.now(), (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(timespec="seconds"), 60, 5, 2, 10, "INR", "confirmed", parkzen.now()))
            conn.commit()
        response = self.client.post("/api/admin/customers/deactivate", headers=self.auth(), json={"user_ids": [user_id], "confirmed": True})
        self.assertEqual(response.status_code, 409)
        with parkzen.APP.app_context():
            conn = parkzen.db()
            booking = conn.execute("SELECT id,slot_id FROM bookings WHERE booking_code='TEST-PROTECTED'").fetchone()
            past = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(timespec="seconds")
            conn.execute("UPDATE bookings SET status='completed',expiry_time=? WHERE id=?", (past, booking["id"]))
            conn.execute("INSERT INTO overstay_cases(booking_id,slot_id,overstay_started_at,rate_per_minute,estimated_penalty,status,penalty_payment_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (booking["id"], booking["slot_id"], past, 1, 10, "awaiting_departure_confirmation", "not_due", past, past))
            conn.commit()
        response = self.client.post("/api/admin/customers/deactivate", headers=self.auth(), json={"user_ids": [user_id], "confirmed": True})
        self.assertEqual(response.status_code, 409)
        with parkzen.APP.app_context(): self.assertEqual(parkzen.db().execute("SELECT is_active FROM users WHERE id=?", (user_id,)).fetchone()[0], 1)

    def test_deactivation_invalidates_session_and_reactivation_restores_login(self):
        user_id, email = self.add_customer("lifecycle")
        customer_token = "feature6-customer-session"
        with parkzen.APP.app_context():
            parkzen.db().execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (customer_token, user_id, parkzen.now())); parkzen.db().commit()
        result = self.client.post("/api/admin/customers/deactivate", headers=self.auth(), json={"user_ids": [user_id], "confirmed": True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.auth(customer_token)).status_code, 401)
        login = self.client.post("/api/auth/login", json={"email": email, "password": "TestPassword123"})
        self.assertEqual(login.status_code, 403)
        self.assertEqual(self.client.post(f"/api/admin/customers/{user_id}/reactivate", headers=self.auth(), json={}).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/login", json={"email": email, "password": "TestPassword123"}).status_code, 200)
        with parkzen.APP.app_context():
            actions = [r[0] for r in parkzen.db().execute("SELECT action FROM admin_audit_log WHERE affected_user_id=? ORDER BY id", (user_id,)).fetchall()]
        self.assertEqual(actions, ["deactivate", "reactivate"])

    def add_weekly_activity(self, user_id, start, today, missed_weeks=()):
        cursor = start - timedelta(days=start.weekday())
        week_number = 0
        with parkzen.APP.app_context():
            conn = parkzen.db()
            while cursor <= today:
                active_day = max(start, cursor)
                if active_day <= today and week_number not in missed_weeks:
                    stamp = datetime.combine(active_day, datetime.min.time(), timezone.utc).isoformat(timespec="seconds")
                    conn.execute("INSERT OR IGNORE INTO loyalty_activity_days(user_id,local_date,first_active_at,last_active_at) VALUES(?,?,?,?)", (user_id, active_day.isoformat(), stamp, stamp))
                cursor += timedelta(days=7)
                week_number += 1
            conn.commit()

    def test_loyalty_calendar_boundaries_and_highest_tier(self):
        today = parkzen.local_today()
        start_30 = today - timedelta(days=29)
        user30, _ = self.add_customer("tier30", created=datetime.combine(start_30, datetime.min.time(), timezone.utc).isoformat())
        self.add_weekly_activity(user30, start_30, today, missed_weeks=(1,))
        start_6 = parkzen.subtract_months(today, 6)
        user6, _ = self.add_customer("tier6", created=datetime.combine(start_6, datetime.min.time(), timezone.utc).isoformat())
        self.add_weekly_activity(user6, start_6, today, missed_weeks=(2,))
        start_12 = parkzen.subtract_months(today, 12)
        user12, _ = self.add_customer("tier12", created=datetime.combine(start_12, datetime.min.time(), timezone.utc).isoformat())
        self.add_weekly_activity(user12, start_12, today, missed_weeks=(3,))
        with parkzen.APP.app_context():
            conn = parkzen.db()
            self.assertEqual(parkzen.loyalty_summary(conn, user30, today)["current_tier"], "30-day")
            self.assertEqual(parkzen.loyalty_summary(conn, user6, today)["current_tier"], "6-month")
            self.assertEqual(parkzen.loyalty_summary(conn, user12, today)["current_tier"], "1-year")

    def test_irregular_activity_does_not_unlock_tier(self):
        today = parkzen.local_today()
        user_id, _ = self.add_customer("irregular", created=(today-timedelta(days=400)).isoformat())
        start = today-timedelta(days=29)
        self.add_weekly_activity(user_id, start, today, missed_weeks=tuple(range(2, 8)))
        with parkzen.APP.app_context():
            summary = parkzen.loyalty_summary(parkzen.db(), user_id, today)
            self.assertFalse(summary["eligible"])
            self.assertEqual(summary["current_tier"], "none")

    def test_repeated_authenticated_actions_count_as_one_active_day(self):
        user_id, email = self.add_customer("daily-login")
        client = parkzen.APP.test_client()
        client.post("/api/auth/login", json={"email": email, "password": "TestPassword123"})
        client.post("/api/auth/login", json={"email": email, "password": "TestPassword123"})
        with parkzen.APP.app_context():
            row = parkzen.db().execute("SELECT COUNT(*),MAX(qualifying_events) FROM loyalty_activity_days WHERE user_id=? AND local_date=?", (user_id, parkzen.qualifying_local_day())).fetchone()
            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], 2)
        self.assertEqual(self.client.get("/api/loyalty").status_code, 401)

    def test_admin_loyalty_endpoint_requires_admin_and_exposes_no_secrets(self):
        self.assertEqual(self.client.get("/api/admin/loyalty").status_code, 401)
        user_id, email = self.add_customer("admin-loyalty-view")
        response = self.client.get("/api/admin/loyalty", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        customer = next(row for row in response.json["customers"] if row["email"] == email)
        self.assertEqual(customer["current_tier"], "none")
        self.assertNotIn("password", customer)

    def test_location_pricing_and_half_up_discount_rounding(self):
        mall_a = parkzen.discounted_booking_quote(5, 50, 1)
        mall_b = parkzen.discounted_booking_quote(8, 50, 1)
        rounding = parkzen.discounted_booking_quote(5, 50, 3)
        half_up = parkzen.discounted_booking_quote("5.01", 50, 1)
        self.assertEqual(mall_a["discounted_rate_paise"], 250)
        self.assertEqual(mall_a["total_amount_paise"], 250)
        self.assertEqual(mall_b["discounted_rate_paise"], 400)
        self.assertEqual(rounding["discount_amount_paise"], 750)
        self.assertEqual(half_up["discounted_rate_paise"], 251)

    def customer_session(self, user_id, token):
        with parkzen.APP.app_context():
            parkzen.db().execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (token, user_id, parkzen.now()))
            parkzen.db().commit()
        return {"Authorization": f"Bearer {token}"}

    def make_booking_hold(self, client, headers, duration_minutes=60):
        with parkzen.APP.app_context():
            conn = parkzen.db()
            slot = conn.execute("SELECT ps.id,ps.level_id FROM parking_slots ps JOIN parking_levels pl ON pl.id=ps.level_id WHERE pl.destination_id='mall-a' AND ps.compatible_vehicle_types LIKE '%car%' LIMIT 1").fetchone()
            conn.execute("UPDATE parking_slots SET status='available',updated_at=? WHERE id=?", (parkzen.now(), slot["id"]))
            conn.commit()
        start = datetime.now(parkzen.LOCAL_ZONE).replace(second=0, microsecond=0)
        start += timedelta(minutes=30-(start.minute % 30))
        expiry = start + timedelta(minutes=duration_minutes)
        with patch.object(parkzen, "RAZORPAY_KEY_ID", "rzp_test"), patch.object(parkzen, "RAZORPAY_KEY_SECRET", "test-secret"), patch.object(parkzen, "razorpay_order", side_effect=lambda amount, receipt, amount_paise=None: ({"id": "order-loyalty", "amount": amount_paise, "currency": "INR"}, None)):
            response = client.post("/api/bookings/hold", headers=headers, json={"level_id": slot["level_id"], "slot_id": slot["id"], "vehicle_type": "car", "start_time": start.isoformat(), "expiry_time": expiry.isoformat(), "discount_percent": 100, "discount_amount": 0})
        return response

    def test_booking_saves_discounted_quote_and_payment_verifies_exact_paise(self):
        today = parkzen.local_today()
        start_date = parkzen.subtract_months(today, 12)
        user_id, email = self.add_customer("booking-loyalty", created=datetime.combine(start_date, datetime.min.time(), timezone.utc).isoformat())
        self.add_weekly_activity(user_id, start_date, today, missed_weeks=(4,))
        headers = self.customer_session(user_id, "loyalty-booking-session")
        client = parkzen.APP.test_client()
        response = self.make_booking_hold(client, headers)
        self.assertEqual(response.status_code, 201, response.json)
        quote = response.json
        self.assertEqual(quote["loyalty_tier"], "1-year")
        self.assertEqual(quote["discount_percent"], 80)
        self.assertEqual(quote["base_rate_per_block"], 5)
        self.assertEqual(quote["rate_per_block_paise"], 100)
        self.assertEqual(quote["total_amount_paise"], 200)
        self.assertEqual(quote["razorpay"]["amount"], 200)
        payment_id = "pay-loyalty"
        signature = hmac.new(b"test-secret", b"order-loyalty|pay-loyalty", hashlib.sha256).hexdigest()
        with patch.object(parkzen, "RAZORPAY_KEY_SECRET", "test-secret"), patch.object(parkzen, "razorpay_payment", return_value=({"order_id": "order-loyalty", "amount": 200, "currency": "INR", "status": "captured"}, None)):
            verified = client.post(f"/api/bookings/{quote['booking_id']}/verify-payment", headers=headers, json={"razorpay_payment_id": payment_id, "razorpay_signature": signature, "method": "upi"})
        self.assertEqual(verified.status_code, 200, verified.json)
        with parkzen.APP.app_context():
            row = parkzen.db().execute("SELECT b.status,b.total_amount_paise,b.discount_amount_paise,p.amount,p.amount_paise,p.status FROM bookings b JOIN payments p ON p.booking_id=b.id WHERE b.id=?", (quote["booking_id"],)).fetchone()
            self.assertEqual(tuple(row), ("confirmed", 200, 800, 2, 200, "verified"))

    def test_expired_hold_rejects_payment_verification(self):
        user_id, _ = self.add_customer("expired-loyalty")
        headers = self.customer_session(user_id, "loyalty-expired-session")
        client = parkzen.APP.test_client()
        response = self.make_booking_hold(client, headers)
        self.assertEqual(response.status_code, 201, response.json)
        with parkzen.APP.app_context():
            expired = (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(timespec="seconds")
            parkzen.db().execute("UPDATE bookings SET hold_expires_at=? WHERE id=?", (expired, response.json["booking_id"]))
            parkzen.db().commit()
        result = client.post(f"/api/bookings/{response.json['booking_id']}/verify-payment", headers=headers, json={})
        self.assertEqual(result.status_code, 409)

    def test_half_rupee_discounted_block_persists_as_250_paise(self):
        today = parkzen.local_today()
        start_date = parkzen.subtract_months(today, 6)
        user_id, _ = self.add_customer("half-rupee", created=datetime.combine(start_date, datetime.min.time(), timezone.utc).isoformat())
        self.add_weekly_activity(user_id, start_date, today, missed_weeks=(5,))
        headers = self.customer_session(user_id, "loyalty-half-rupee")
        response = self.make_booking_hold(parkzen.APP.test_client(), headers, duration_minutes=30)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["loyalty_tier"], "6-month")
        self.assertEqual(response.json["rate_per_block_paise"], 250)
        self.assertEqual(response.json["total_amount_paise"], 250)
        self.assertEqual(response.json["razorpay"]["amount"], 250)
        with parkzen.APP.app_context():
            row = parkzen.db().execute("SELECT b.total_amount,b.total_amount_paise,p.amount,p.amount_paise FROM bookings b JOIN payments p ON p.booking_id=b.id WHERE b.id=?", (response.json["booking_id"],)).fetchone()
            self.assertEqual(tuple(row), (2.5, 250, 2.5, 250))

    def test_concurrent_booking_holds_for_same_slot_only_one_succeeds(self):
        user_a, _ = self.add_customer("booking-race-a")
        user_b, _ = self.add_customer("booking-race-b")
        client_a = parkzen.APP.test_client(); client_b = parkzen.APP.test_client()
        headers_a = self.customer_session(user_a, "loyalty-race-a")
        headers_b = self.customer_session(user_b, "loyalty-race-b")
        barrier = threading.Barrier(2); results = []
        def place(client, headers):
            barrier.wait()
            results.append(self.make_booking_hold(client, headers).status_code)
        with patch.object(parkzen, "RAZORPAY_KEY_ID", "rzp_test"), patch.object(parkzen, "RAZORPAY_KEY_SECRET", "test-secret"), patch.object(parkzen, "razorpay_order", return_value=({"id": "order-race", "amount": 500, "currency": "INR"}, None)):
            a = threading.Thread(target=place, args=(client_a, headers_a)); b = threading.Thread(target=place, args=(client_b, headers_b))
            a.start(); b.start(); a.join(15); b.join(15)
        self.assertEqual(sorted(results), [201, 409])


if __name__ == "__main__":
    unittest.main()

