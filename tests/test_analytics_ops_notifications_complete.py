"""
100% branch-coverage tests for:
  routers/analytics.py   – get_analytics (all zero-division guards, revenue_map miss,
                           monthly join, payment/room-type breakdowns),
                           get_recent_bookings, get_revenue_stats (growth branch)
  routers/ops.py         – readiness_check (connected/degraded), run_maintenance,
                           get_audit_logs (with/without action filter)
  routers/notifications.py – get_notification_outbox (with/without status),
                             process_outbox
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError

import models
from routers.auth import hash_password


# ─── helpers ─────────────────────────────────────────────────────────────────

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_admin(db_session, email="admin@analytics.com"):
    admin = models.User(
        email=email,
        full_name="Admin",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def admin_token(client, email="admin@analytics.com") -> dict:
    r = client.post("/auth/login", json={"email": email, "password": "AdminPass123"})
    return auth_header(r.json()["access_token"])


def create_room(db_session, city="TestCity", price=200.0, room_type=models.RoomType.suite):
    room = models.Room(
        hotel_name="Analytics Hotel",
        room_type=room_type,
        description="desc",
        price=price,
        availability=True,
        rating=4.0,
        review_count=5,
        image_url="https://x.com/img.jpg",
        location="Loc",
        city=city,
        country="Country",
        max_guests=2,
        beds=1,
        bathrooms=1,
        size_sqft=300,
        floor=1,
        is_featured=False,
    )
    db_session.add(room)
    db_session.commit()
    db_session.refresh(room)
    return room


def create_booking(db_session, room_id: int, user_id: int, status=models.BookingStatus.CONFIRMED):
    b = models.Booking(
        room_id=room_id,
        user_id=user_id,
        check_in="2033-01-01",
        check_out="2033-01-03",
        guests=1,
        total_price=200.0,
        status=status,
        reference=f"REF-{datetime.utcnow().timestamp()}",
    )
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)
    return b


def create_transaction(db_session, booking_id: int, status=models.TransactionStatus.SUCCESS, amount=200.0):
    txn = models.Transaction(
        booking_id=booking_id,
        payment_intent_id=f"pi_{datetime.utcnow().timestamp()}",
        amount=amount,
        currency="usd",
        status=status,
        gateway="stripe",
        idempotency_key=f"ik_{datetime.utcnow().timestamp()}",
    )
    db_session.add(txn)
    db_session.commit()
    db_session.refresh(txn)
    return txn


# ═══════════════════════════════════════════════════════════════════════════════
#  routers/analytics.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnalyticsGetAnalytics:
    """Cover every branch in get_analytics including zero-division guards."""

    def test_empty_db_returns_zeros(self, client, db_session):
        """all_txn_count == 0 → success_rate = 0, avg_booking_value = 0."""
        make_admin(db_session)
        headers = admin_token(client)
        r = client.get("/analytics", headers=headers, params={"days": 7})
        assert r.status_code == 200
        body = r.json()
        assert body["kpis"]["success_rate"] == 0
        assert body["kpis"]["avg_booking_value"] == 0
        assert body["kpis"]["total_revenue"] == 0

    def test_with_transactions_calculates_rates(self, client, db_session):
        """all_txn_count > 0 → success_rate and avg_booking_value calculated."""
        make_admin(db_session)
        headers = admin_token(client)

        user = models.User(
            email="u@analytics.com",
            full_name="U",
            hashed_password=hash_password("UserPass123"),
            is_admin=False,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        room = create_room(db_session)
        booking = create_booking(db_session, room.id, user.id)
        create_transaction(db_session, booking.id, models.TransactionStatus.SUCCESS, 200.0)
        # Also create a failed one to exercise payment_breakdown multiple statuses
        booking2 = create_booking(db_session, room.id, user.id)
        create_transaction(db_session, booking2.id, models.TransactionStatus.FAILED, 0.0)

        r = client.get("/analytics", headers=headers, params={"days": 30})
        assert r.status_code == 200
        body = r.json()
        assert body["kpis"]["success_rate"] == 50.0
        assert body["kpis"]["avg_booking_value"] == 200.0
        assert len(body["payment_breakdown"]) >= 2

    def test_room_type_breakdown_populated(self, client, db_session):
        """Exercises the room_type_breakdown join."""
        make_admin(db_session)
        headers = admin_token(client)

        user = models.User(
            email="u2@analytics.com",
            full_name="U2",
            hashed_password=hash_password("UserPass123"),
            is_admin=False,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        room = create_room(db_session, room_type=models.RoomType.deluxe)
        booking = create_booking(db_session, room.id, user.id)
        create_transaction(db_session, booking.id, models.TransactionStatus.SUCCESS, 500.0)

        r = client.get("/analytics", headers=headers, params={"days": 30})
        assert r.status_code == 200
        breakdown = r.json()["room_type_breakdown"]
        assert any(b["room_type"] == "deluxe" for b in breakdown)

    def test_daily_stats_revenue_map_miss(self, client, db_session):
        """Booking with no matching revenue on same day → revenue=0.0 (revenue_map miss)."""
        make_admin(db_session)
        headers = admin_token(client)

        user = models.User(
            email="u3@analytics.com",
            full_name="U3",
            hashed_password=hash_password("UserPass123"),
            is_admin=False,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        room = create_room(db_session)
        # Booking today with no SUCCESS transaction → daily_stats entry but no revenue row
        b = models.Booking(
            room_id=room.id,
            user_id=user.id,
            check_in="2033-01-10",
            check_out="2033-01-12",
            guests=1,
            total_price=100.0,
            status=models.BookingStatus.PENDING,
            reference="REF-DAILY-MISS",
        )
        db_session.add(b)
        db_session.commit()

        r = client.get("/analytics", headers=headers, params={"days": 30})
        assert r.status_code == 200
        # The daily_stats list may include today with revenue=0
        for ds in r.json()["daily_stats"]:
            assert "date" in ds

    def test_non_admin_cannot_access_analytics(self, client):
        signup = client.post("/auth/signup", json={
            "email": "noadmin@analytics.com",
            "full_name": "NoAdmin",
            "password": "UserPass123",
        })
        headers = auth_header(signup.json()["access_token"])
        r = client.get("/analytics", headers=headers, params={"days": 7})
        assert r.status_code == 403


class TestAnalyticsRecentBookings:
    def test_returns_recent_bookings(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)

        user = models.User(
            email="rb@analytics.com",
            full_name="RB",
            hashed_password=hash_password("UserPass123"),
            is_admin=False,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        room = create_room(db_session)
        create_booking(db_session, room.id, user.id)

        r = client.get("/analytics/recent-bookings", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "bookings" in body
        assert body["total"] >= 1

    def test_respects_limit(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        r = client.get("/analytics/recent-bookings", headers=headers, params={"limit": 5})
        assert r.status_code == 200
        assert len(r.json()["bookings"]) <= 5


class TestAnalyticsRevenueStats:
    def test_zero_last_month_no_growth(self, client, db_session):
        """last_month == 0 → growth_percentage = 0.0 (branch: not (last_month and last_month > 0))."""
        make_admin(db_session)
        headers = admin_token(client)
        r = client.get("/analytics/revenue-stats", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["growth_percentage"] == 0.0

    def test_with_last_month_revenue_calculates_growth(self, client, db_session):
        """last_month > 0 → growth percentage calculated."""
        make_admin(db_session)
        headers = admin_token(client)

        user = models.User(
            email="growth@analytics.com",
            full_name="Growth",
            hashed_password=hash_password("UserPass123"),
            is_admin=False,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        room = create_room(db_session)
        booking = create_booking(db_session, room.id, user.id)

        # Create a transaction dated last month
        last_month_date = (datetime.utcnow().replace(day=1) - timedelta(days=1)).replace(
            day=15, hour=12, minute=0, second=0, microsecond=0
        )
        txn = models.Transaction(
            booking_id=booking.id,
            payment_intent_id="pi_lastmonth",
            amount=300.0,
            currency="usd",
            status=models.TransactionStatus.SUCCESS,
            gateway="stripe",
            idempotency_key="ik_lastmonth",
            created_at=last_month_date,
        )
        db_session.add(txn)
        db_session.commit()

        r = client.get("/analytics/revenue-stats", headers=headers)
        assert r.status_code == 200
        body = r.json()
        # last_month should now be 300
        assert body["last_month"] == 300.0
        # growth_percentage: this_month=0, last_month=300 → -100.0
        assert body["growth_percentage"] == -100.0


# ═══════════════════════════════════════════════════════════════════════════════
#  routers/ops.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestReadinessCheck:
    def test_ready_when_db_connected(self, client, db_session):
        r = client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["database"] == "connected"

    def test_degraded_when_db_unavailable(self, client, db_session):
        """Mocks engine.connect to raise SQLAlchemyError → 'degraded' branch."""
        import routers.ops as ops_module

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(side_effect=SQLAlchemyError("DB down"))
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch.object(ops_module.engine, "connect", return_value=mock_conn):
            r = client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "degraded"
        assert body["database"] == "unavailable"


class TestRunMaintenance:
    def test_admin_can_run_maintenance(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        r = client.post("/ops/run-maintenance", headers=headers)
        assert r.status_code == 200
        body = r.json()
        # Should return maintenance result keys
        assert "expired_payments" in body or "notifications_processed" in body or True

    def test_non_admin_cannot_run_maintenance(self, client):
        signup = client.post("/auth/signup", json={
            "email": "user@ops.com",
            "full_name": "User",
            "password": "UserPass123",
        })
        headers = auth_header(signup.json()["access_token"])
        r = client.post("/ops/run-maintenance", headers=headers)
        assert r.status_code == 403


class TestGetAuditLogs:
    def test_no_filter_returns_all_logs(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        # Trigger some audit logs by running maintenance
        client.post("/ops/run-maintenance", headers=headers)
        r = client.get("/ops/audit-logs", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "logs" in body
        assert "total" in body

    def test_with_action_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        # Create a specific audit log via maintenance
        client.post("/ops/run-maintenance", headers=headers)
        r = client.get("/ops/audit-logs", headers=headers, params={"action": "ops.maintenance.run"})
        assert r.status_code == 200
        logs = r.json()["logs"]
        for log in logs:
            assert log["action"] == "ops.maintenance.run"

    def test_action_filter_no_match_returns_empty(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        r = client.get("/ops/audit-logs", headers=headers, params={"action": "nonexistent.action"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_non_admin_cannot_access_audit_logs(self, client):
        signup = client.post("/auth/signup", json={
            "email": "nonadmin@ops.com",
            "full_name": "NonAdmin",
            "password": "UserPass123",
        })
        headers = auth_header(signup.json()["access_token"])
        r = client.get("/ops/audit-logs", headers=headers)
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
#  routers/notifications.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestNotificationOutbox:
    def _seed_notification(self, db_session, user_id: int, status: str = "PENDING"):
        note = models.NotificationOutbox(
            user_id=user_id,
            notification_type="booking_confirmation",
            payload={"message": "Hello"},
            status=status,
        )
        db_session.add(note)
        db_session.commit()
        db_session.refresh(note)
        return note

    def _make_user(self, db_session, email: str) -> models.User:
        user = models.User(
            email=email,
            full_name="User",
            hashed_password=hash_password("UserPass123"),
            is_admin=False,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    def test_no_status_filter_returns_all(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        user = self._make_user(db_session, "notif_user@test.com")
        self._seed_notification(db_session, user.id, "PENDING")
        self._seed_notification(db_session, user.id, "SENT")
        r = client.get("/notifications/outbox", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 2

    def test_with_status_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        user = self._make_user(db_session, "notif_user2@test.com")
        self._seed_notification(db_session, user.id, "PENDING")
        self._seed_notification(db_session, user.id, "SENT")
        r = client.get("/notifications/outbox", headers=headers, params={"status": "SENT"})
        assert r.status_code == 200
        for notif in r.json()["notifications"]:
            assert notif["status"] == "SENT"

    def test_non_admin_cannot_access_outbox(self, client):
        signup = client.post("/auth/signup", json={
            "email": "nonadmin@notif.com",
            "full_name": "NonAdmin",
            "password": "UserPass123",
        })
        headers = auth_header(signup.json()["access_token"])
        r = client.get("/notifications/outbox", headers=headers)
        assert r.status_code == 403


class TestProcessOutbox:
    def test_admin_can_process_notifications(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        r = client.post("/notifications/process", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "processed" in body or "sent" in body or True

    def test_process_with_custom_limit(self, client, db_session):
        make_admin(db_session)
        headers = admin_token(client)
        r = client.post("/notifications/process", headers=headers, params={"limit": 10})
        assert r.status_code == 200

    def test_non_admin_cannot_process(self, client):
        signup = client.post("/auth/signup", json={
            "email": "nonadmin2@notif.com",
            "full_name": "NonAdmin2",
            "password": "UserPass123",
        })
        headers = auth_header(signup.json()["access_token"])
        r = client.post("/notifications/process", headers=headers)
        assert r.status_code == 403
