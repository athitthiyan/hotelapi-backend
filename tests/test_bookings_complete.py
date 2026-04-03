"""
100% branch-coverage tests for routers/bookings.py
Covers all branches in helper functions and every HTTP endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from routers.auth import hash_password
from routers.bookings import (
    normalize_comparison_datetime,
    expire_stale_booking_hold,
    calculate_booking_amount,
    generate_booking_ref,
)


# ─── helpers ─────────────────────────────────────────────────────────────────

FUTURE_IN = datetime.now(timezone.utc) + timedelta(days=60)
FUTURE_OUT = FUTURE_IN + timedelta(days=2)

PAST_IN = datetime(2020, 1, 1, tzinfo=timezone.utc)
PAST_OUT = datetime(2020, 1, 3, tzinfo=timezone.utc)


def booking_payload(room_id: int, **overrides) -> dict:
    payload = {
        "room_id": room_id,
        "user_name": "Athit",
        "email": "athit@example.com",
        "phone": "9876543210",
        "check_in": FUTURE_IN.isoformat(),
        "check_out": FUTURE_OUT.isoformat(),
        "guests": 2,
        "special_requests": "",
    }
    payload.update(overrides)
    return payload


def admin_headers(client, db_session) -> dict:
    admin = models.User(
        email="admin-book@example.com",
        full_name="Admin",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-book@example.com", "password": "AdminPass123"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ─── Unit tests for pure helpers ──────────────────────────────────────────────


class TestBookingHelpers:
    def test_normalize_naive_to_aware(self):
        naive = datetime(2030, 1, 1)
        aware = datetime(2030, 1, 1, tzinfo=timezone.utc)
        result = normalize_comparison_datetime(naive, aware)
        assert result.tzinfo is not None

    def test_normalize_aware_to_naive(self):
        aware = datetime(2030, 1, 1, tzinfo=timezone.utc)
        naive = datetime(2030, 1, 1)
        result = normalize_comparison_datetime(aware, naive)
        assert result.tzinfo is None

    def test_normalize_both_same_type_returns_unchanged(self):
        aware1 = datetime(2030, 1, 1, tzinfo=timezone.utc)
        aware2 = datetime(2030, 1, 2, tzinfo=timezone.utc)
        result = normalize_comparison_datetime(aware1, aware2)
        assert result == aware1

    def test_calculate_booking_amount(self):
        room = models.Room(price=200.0)
        rate, taxes, fee, total = calculate_booking_amount(room, 3)
        assert rate == 600.0
        assert taxes == round(600.0 * 0.12, 2)
        assert fee == round(600.0 * 0.05, 2)
        assert total == round(rate + taxes + fee, 2)

    def test_generate_booking_ref_format(self):
        ref = generate_booking_ref()
        assert ref.startswith("BK")
        assert len(ref) == 10  # "BK" + 8 chars

    def test_expire_stale_booking_hold_returns_true_when_expired(self):
        booking = models.Booking(
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
            hold_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        result = expire_stale_booking_hold(booking)
        assert result is True
        assert booking.status == models.BookingStatus.EXPIRED
        assert booking.payment_status == models.PaymentStatus.EXPIRED

    def test_expire_stale_hold_returns_false_when_not_expired(self):
        booking = models.Booking(
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
            hold_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        result = expire_stale_booking_hold(booking)
        assert result is False

    def test_expire_stale_hold_returns_false_when_hold_expires_at_none(self):
        booking = models.Booking(
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
            hold_expires_at=None,
        )
        result = expire_stale_booking_hold(booking)
        assert result is False

    def test_expire_stale_hold_returns_false_when_already_confirmed(self):
        booking = models.Booking(
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
            hold_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        result = expire_stale_booking_hold(booking)
        assert result is False

    def test_expire_stale_hold_returns_false_when_paid(self):
        booking = models.Booking(
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PAID,
            hold_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        result = expire_stale_booking_hold(booking)
        assert result is False

    def test_expire_stale_hold_normalizes_naive_expires_at(self):
        """hold_expires_at is naive but now is aware — should still compare correctly."""
        booking = models.Booking(
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
            hold_expires_at=datetime(2020, 1, 1),  # naive, clearly past
        )
        now_aware = datetime.now(timezone.utc)
        result = expire_stale_booking_hold(booking, now=now_aware)
        assert result is True


# ─── Create booking endpoint ──────────────────────────────────────────────────


class TestCreateBooking:
    def test_create_booking_success_201(self, client, room_id):
        r = client.post("/bookings", json=booking_payload(room_id))
        assert r.status_code == 201
        body = r.json()
        assert body["room_id"] == room_id
        assert body["status"] == "pending"

    def test_room_not_found_returns_404(self, client):
        r = client.post("/bookings", json=booking_payload(9999))
        assert r.status_code == 404
        assert r.json()["detail"] == "Room not found"

    def test_unavailable_room_returns_400(self, client, db_session):
        room = models.Room(
            hotel_name="Closed Hotel",
            room_type=models.RoomType.STANDARD,
            description="closed",
            price=100.0,
            availability=False,
            city="X",
            country="Y",
        )
        db_session.add(room)
        db_session.commit()
        db_session.refresh(room)

        r = client.post("/bookings", json=booking_payload(room.id))
        assert r.status_code == 400
        assert r.json()["detail"] == "Room is not available"

    def test_checkout_before_checkin_returns_400(self, client, room_id):
        r = client.post(
            "/bookings",
            json=booking_payload(
                room_id,
                check_in=FUTURE_OUT.isoformat(),
                check_out=FUTURE_IN.isoformat(),  # reversed
            ),
        )
        assert r.status_code == 400
        assert "Check-out must be after check-in" in r.json()["detail"]

    def test_same_day_checkin_checkout_returns_400(self, client, room_id):
        same = FUTURE_IN.isoformat()
        r = client.post("/bookings", json=booking_payload(room_id, check_in=same, check_out=same))
        assert r.status_code == 400

    def test_overlapping_dates_returns_409(self, client, room_id):
        client.post("/bookings", json=booking_payload(room_id))
        # Same dates — overlap
        r = client.post("/bookings", json=booking_payload(room_id))
        assert r.status_code == 409

    def test_invalid_phone_returns_422(self, client, room_id):
        r = client.post(
            "/bookings",
            json=booking_payload(room_id, phone="not-a-phone"),
        )
        assert r.status_code == 422


# ─── Get bookings endpoint ────────────────────────────────────────────────────


class TestGetBookings:
    def test_get_bookings_empty(self, client):
        r = client.get("/bookings")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_get_bookings_returns_list(self, client, room_id):
        client.post("/bookings", json=booking_payload(room_id))
        r = client.get("/bookings")
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    def test_get_bookings_filter_by_email(self, client, room_id):
        client.post("/bookings", json=booking_payload(room_id))
        r = client.get("/bookings", params={"email": "athit@example.com"})
        assert r.status_code == 200
        bookings = r.json()["bookings"]
        assert all(b["email"] == "athit@example.com" for b in bookings)

    def test_get_bookings_filter_by_status(self, client, room_id):
        client.post("/bookings", json=booking_payload(room_id))
        r = client.get("/bookings", params={"status": "pending"})
        assert r.status_code == 200
        bookings = r.json()["bookings"]
        assert all(b["status"] == "pending" for b in bookings)

    def test_get_bookings_filter_by_email_and_status(self, client, room_id):
        client.post("/bookings", json=booking_payload(room_id))
        r = client.get("/bookings", params={"email": "athit@example.com", "status": "pending"})
        assert r.status_code == 200

    def test_get_bookings_pagination(self, client, room_id):
        r = client.get("/bookings", params={"page": 1, "per_page": 5})
        assert r.status_code == 200


# ─── Get booking by ID ────────────────────────────────────────────────────────


class TestGetBookingById:
    def test_get_booking_by_id_success(self, client, room_id):
        create_r = client.post("/bookings", json=booking_payload(room_id))
        booking_id = create_r.json()["id"]
        r = client.get(f"/bookings/{booking_id}")
        assert r.status_code == 200
        assert r.json()["id"] == booking_id

    def test_get_booking_not_found_returns_404(self, client):
        r = client.get("/bookings/999999")
        assert r.status_code == 404

    def test_get_expired_booking_returns_expired_status(self, client, db_session, room_id):
        """Booking with past hold_expires_at gets expired on retrieval."""
        booking = models.Booking(
            booking_ref="BK-EXPTEST1",
            user_name="Athit",
            email="athit@example.com",
            phone="1234567890",
            room_id=room_id,
            check_in=FUTURE_IN,
            check_out=FUTURE_OUT,
            hold_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # past
            guests=2,
            nights=2,
            room_rate=200.0,
            taxes=48.0,
            service_fee=20.0,
            total_amount=268.0,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        db_session.add(booking)
        db_session.commit()
        db_session.refresh(booking)

        r = client.get(f"/bookings/{booking.id}")
        assert r.status_code == 200
        assert r.json()["status"] == "expired"


# ─── Get booking by ref ───────────────────────────────────────────────────────


class TestGetBookingByRef:
    def test_get_booking_by_ref_success(self, client, room_id):
        create_r = client.post("/bookings", json=booking_payload(room_id))
        ref = create_r.json()["booking_ref"]
        r = client.get(f"/bookings/ref/{ref}")
        assert r.status_code == 200
        assert r.json()["booking_ref"] == ref

    def test_get_booking_by_ref_not_found(self, client):
        r = client.get("/bookings/ref/BK-INVALID")
        assert r.status_code == 404

    def test_get_booking_by_ref_expires_stale(self, client, db_session, room_id):
        booking = models.Booking(
            booking_ref="BK-STALEREF1",
            user_name="Athit",
            email="athit@example.com",
            phone="1234567890",
            room_id=room_id,
            check_in=FUTURE_IN,
            check_out=FUTURE_OUT,
            hold_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=200.0,
            taxes=48.0,
            service_fee=20.0,
            total_amount=268.0,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        db_session.add(booking)
        db_session.commit()
        db_session.refresh(booking)

        r = client.get(f"/bookings/ref/BK-STALEREF1")
        assert r.status_code == 200
        assert r.json()["status"] == "expired"


# ─── Booking history ──────────────────────────────────────────────────────────


class TestBookingHistory:
    def test_get_history_returns_bookings(self, client, room_id):
        client.post("/bookings", json=booking_payload(room_id))
        r = client.get("/bookings/history", params={"email": "athit@example.com"})
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    def test_get_history_empty_for_unknown_email(self, client):
        r = client.get("/bookings/history", params={"email": "nobody@example.com"})
        assert r.status_code == 200
        assert r.json()["total"] == 0


# ─── Cancel booking ───────────────────────────────────────────────────────────


class TestCancelBooking:
    def test_cancel_pending_booking_success(self, client, room_id):
        create_r = client.post("/bookings", json=booking_payload(room_id))
        booking_id = create_r.json()["id"]
        r = client.patch(f"/bookings/{booking_id}/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_cancel_not_found_returns_404(self, client):
        r = client.patch("/bookings/999999/cancel")
        assert r.status_code == 404

    def test_cancel_already_cancelled_returns_400(self, client, room_id):
        create_r = client.post("/bookings", json=booking_payload(room_id))
        booking_id = create_r.json()["id"]
        client.patch(f"/bookings/{booking_id}/cancel")
        r = client.patch(f"/bookings/{booking_id}/cancel")
        assert r.status_code == 400
        assert r.json()["detail"] == "Booking already cancelled"

    def test_cancel_already_expired_returns_400(self, client, db_session, room_id):
        booking = models.Booking(
            booking_ref="BK-EXPCANC1",
            user_name="Athit",
            email="athit@example.com",
            phone="1234567890",
            room_id=room_id,
            check_in=FUTURE_IN,
            check_out=FUTURE_OUT,
            hold_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=200.0,
            taxes=48.0,
            service_fee=20.0,
            total_amount=268.0,
            status=models.BookingStatus.EXPIRED,
            payment_status=models.PaymentStatus.EXPIRED,
        )
        db_session.add(booking)
        db_session.commit()
        db_session.refresh(booking)

        r = client.patch(f"/bookings/{booking.id}/cancel")
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()

    def test_cancel_stale_hold_returns_400_expired(self, client, db_session, room_id):
        """Booking hold expired at cancel time → mark expired and return 400."""
        booking = models.Booking(
            booking_ref="BK-EXPCANC2",
            user_name="Athit",
            email="athit@example.com",
            phone="1234567890",
            room_id=room_id,
            check_in=FUTURE_IN,
            check_out=FUTURE_OUT,
            hold_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # past
            guests=1,
            nights=2,
            room_rate=200.0,
            taxes=48.0,
            service_fee=20.0,
            total_amount=268.0,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        db_session.add(booking)
        db_session.commit()
        db_session.refresh(booking)

        r = client.patch(f"/bookings/{booking.id}/cancel")
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()

    def test_cancel_paid_booking_returns_400(self, client, db_session, room_id):
        booking = models.Booking(
            booking_ref="BK-PAIDCANC",
            user_name="Athit",
            email="athit@example.com",
            phone="1234567890",
            room_id=room_id,
            check_in=FUTURE_IN,
            check_out=FUTURE_OUT,
            hold_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=200.0,
            taxes=48.0,
            service_fee=20.0,
            total_amount=268.0,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        db_session.add(booking)
        db_session.commit()
        db_session.refresh(booking)

        r = client.patch(f"/bookings/{booking.id}/cancel")
        assert r.status_code == 400
        assert "refund" in r.json()["detail"].lower() or "paid" in r.json()["detail"].lower()


# ─── Admin dashboard ──────────────────────────────────────────────────────────


class TestAdminBookingDashboard:
    def test_dashboard_requires_admin(self, client):
        r = client.get("/bookings/admin/dashboard")
        assert r.status_code == 401

    def test_dashboard_returns_counts(self, client, db_session, room_id):
        headers = admin_headers(client, db_session)
        r = client.get("/bookings/admin/dashboard", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "total" in body
        assert "pending_count" in body
        assert "confirmed_count" in body
        assert "failed_payment_count" in body

    def test_dashboard_filters_by_email(self, client, db_session, room_id):
        headers = admin_headers(client, db_session)
        client.post("/bookings", json=booking_payload(room_id))
        r = client.get(
            "/bookings/admin/dashboard",
            headers=headers,
            params={"email": "athit@example.com"},
        )
        assert r.status_code == 200
        assert r.json()["total"] >= 0

    def test_dashboard_filters_by_status(self, client, db_session, room_id):
        headers = admin_headers(client, db_session)
        r = client.get(
            "/bookings/admin/dashboard",
            headers=headers,
            params={"status": "pending"},
        )
        assert r.status_code == 200

    def test_dashboard_filters_by_payment_status(self, client, db_session, room_id):
        headers = admin_headers(client, db_session)
        r = client.get(
            "/bookings/admin/dashboard",
            headers=headers,
            params={"payment_status": "pending"},
        )
        assert r.status_code == 200

    def test_dashboard_pagination(self, client, db_session, room_id):
        headers = admin_headers(client, db_session)
        r = client.get(
            "/bookings/admin/dashboard",
            headers=headers,
            params={"page": 1, "per_page": 5},
        )
        assert r.status_code == 200
