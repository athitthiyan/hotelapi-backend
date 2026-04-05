"""Tests for Phase 5: Backend validation hardening + structured error codes."""

from datetime import datetime, timedelta, timezone

import models


def booking_payload(room_id: int, **overrides):
    payload = {
        "user_name": "Athit",
        "email": "athit@example.com",
        "phone": "1234567890",
        "room_id": room_id,
        "check_in": datetime.now(timezone.utc).isoformat(),
        "check_out": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "guests": 2,
        "special_requests": "",
    }
    payload.update(overrides)
    return payload


# ─── Test 1: Structured error for room not found ──────────────────────────────

def test_create_booking_returns_structured_error_room_not_found(client):
    """POST /bookings with fake room_id → expect 404 with ROOM_NOT_FOUND code."""
    response = client.post("/bookings", json=booking_payload(999999))
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "ROOM_NOT_FOUND"
    assert "message" in detail
    assert "Room not found" in detail["message"]


# ─── Test 2: Structured error for invalid date range ───────────────────────────

def test_create_booking_returns_structured_error_invalid_date_range(client, room_id):
    """check_out before check_in → expect 400 with INVALID_DATE_RANGE code."""
    now = datetime.now(timezone.utc)
    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=now.isoformat(),
            check_out=(now - timedelta(days=1)).isoformat(),
        ),
    )
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "INVALID_DATE_RANGE"
    assert "message" in detail
    assert "after check-in" in detail["message"]
    assert detail.get("field") == "check_out"


# ─── Test 3: Check-in in past is rejected ─────────────────────────────────────

def test_create_booking_check_in_in_past_rejected(client, room_id):
    """check_in yesterday → expect 400 with CHECK_IN_PAST code."""
    now = datetime.now(timezone.utc)
    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=(now - timedelta(days=1)).isoformat(),
            check_out=(now + timedelta(days=1)).isoformat(),
        ),
    )
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "CHECK_IN_PAST"
    assert "message" in detail
    assert "future" in detail["message"]
    assert detail.get("field") == "check_in"


# ─── Test 4: Guest count exceeds capacity ─────────────────────────────────────

def test_create_booking_guest_count_exceeds_capacity(client, room_id):
    """guests=99 on a room with max_guests=2 → expect 400 with GUEST_CAPACITY_EXCEEDED code."""
    response = client.post(
        "/bookings",
        json=booking_payload(room_id, guests=99),
    )
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "GUEST_CAPACITY_EXCEEDED"
    assert "message" in detail
    assert "accommodates a maximum of" in detail["message"]
    assert detail.get("field") == "guests"


# ─── Test 5: Room unavailable (availability=False) ────────────────────────────

def test_create_booking_room_unavailable(client, db_session, room_id):
    """set room.availability=False → expect 400 with ROOM_UNAVAILABLE code."""
    room = db_session.query(models.Room).filter_by(id=room_id).first()
    room.availability = False
    db_session.commit()

    response = client.post("/bookings", json=booking_payload(room_id))
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "ROOM_UNAVAILABLE"
    assert "message" in detail
    assert "not currently available" in detail["message"]


# ─── Test 6: Duplicate active hold is rejected ─────────────────────────────────

def test_create_booking_duplicate_hold_rejected(client, room_id):
    """create booking, then immediately try to create same booking again → expect 409 with HOLD_EXISTS code."""
    now = datetime.now(timezone.utc)
    check_in = (now + timedelta(hours=2)).isoformat()
    check_out = (now + timedelta(days=2)).isoformat()

    # Create first booking
    payload = booking_payload(room_id, check_in=check_in, check_out=check_out)
    first = client.post("/bookings", json=payload)
    assert first.status_code == 201

    # Try to create a second booking for the exact same dates
    payload2 = booking_payload(
        room_id,
        check_in=check_in,
        check_out=check_out,
        email="different@example.com",
    )
    second = client.post("/bookings", json=payload2)
    assert second.status_code == 409
    data = second.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "HOLD_EXISTS"
    assert "message" in detail
    assert "active booking hold" in detail["message"]
    assert detail.get("field") == "date_range"


# ─── Test 7: Cancel non-existent booking ──────────────────────────────────────

def test_cancel_booking_returns_structured_error(client):
    """cancel a non-existent booking → expect 404 with HOLD_NOT_FOUND code."""
    response = client.patch("/bookings/999999/cancel")
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "HOLD_NOT_FOUND"
    assert "message" in detail


# ─── Test 8: Cancel already-cancelled booking ─────────────────────────────────

def test_cancel_booking_already_cancelled(client, db_session, room_id):
    """cancel same booking twice → expect 400 with HOLD_EXPIRED code."""
    # Create a booking
    response = client.post("/bookings", json=booking_payload(room_id))
    assert response.status_code == 201
    booking_id = response.json()["id"]

    # Cancel it once
    first_cancel = client.patch(f"/bookings/{booking_id}/cancel")
    assert first_cancel.status_code == 200

    # Try to cancel again
    second_cancel = client.patch(f"/bookings/{booking_id}/cancel")
    assert second_cancel.status_code == 400
    data = second_cancel.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "HOLD_EXPIRED"
    assert "message" in detail
    assert "already been cancelled" in detail["message"]


# ─── Test 9: Error response has required fields ────────────────────────────────

def test_error_response_has_code_message_field_structure(client):
    """verify error responses always have code, message, and optionally field."""
    # Test with a room not found error
    response = client.post("/bookings", json=booking_payload(999999))
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data
    detail = data["detail"]

    # Check required fields
    assert isinstance(detail, dict)
    assert "code" in detail
    assert isinstance(detail["code"], str)
    assert len(detail["code"]) > 0

    assert "message" in detail
    assert isinstance(detail["message"], str)
    assert len(detail["message"]) > 0

    # field is optional
    if "field" in detail:
        assert isinstance(detail["field"], (str, type(None)))


# ─── Test 10: Check-in at current time is rejected (must be in future) ────────

def test_create_booking_check_in_at_current_time_rejected(client, room_id):
    """check_in at exactly now → expect 400 with CHECK_IN_PAST code."""
    now = datetime.now(timezone.utc)
    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=now.isoformat(),
            check_out=(now + timedelta(days=2)).isoformat(),
        ),
    )
    assert response.status_code == 400
    data = response.json()
    detail = data["detail"]
    assert detail["code"] == "CHECK_IN_PAST"


# ─── Test 11: Room capacity with max_guests edge case ────────────────────────

def test_create_booking_with_exact_max_guests_capacity(client, db_session, room_id):
    """guests equal to room.max_guests should succeed (assuming room has max_guests=2)."""
    room = db_session.query(models.Room).filter_by(id=room_id).first()
    # Room fixture has max_guests=2 by default
    assert room.max_guests == 2

    response = client.post(
        "/bookings",
        json=booking_payload(room_id, guests=2),
    )
    # Should succeed with guests=2 (equal to max)
    assert response.status_code == 201


# ─── Test 12: Booking error detail field structure ────────────────────────────

def test_booking_error_detail_field_set_correctly(client, room_id):
    """error with a field should include it in the response."""
    now = datetime.now(timezone.utc)
    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=now.isoformat(),
            check_out=(now - timedelta(days=1)).isoformat(),
        ),
    )
    assert response.status_code == 400
    data = response.json()
    detail = data["detail"]
    assert "field" in detail
    assert detail["field"] == "check_out"


# ─── Test 13: Extend hold with wrong email ────────────────────────────────────

def test_extend_hold_email_mismatch_structured_error(client, room_id):
    """extend hold with wrong email → expect 403 with AUTH_REQUIRED code."""
    response = client.post("/bookings", json=booking_payload(room_id))
    assert response.status_code == 201
    booking_id = response.json()["id"]

    response = client.post(
        f"/bookings/{booking_id}/extend-hold",
        json={"email": "wrong@example.com"},
    )
    assert response.status_code == 403
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "AUTH_REQUIRED"
    assert "message" in detail
    assert "Email" in detail["message"]


# ─── Test 14: Extend hold on confirmed paid booking ────────────────────────────

def test_extend_hold_on_paid_booking_returns_duplicate_booking(client, db_session, room_id):
    """extending hold on a paid booking → expect 409 with DUPLICATE_BOOKING code."""
    response = client.post("/bookings", json=booking_payload(room_id))
    assert response.status_code == 201
    booking_id = response.json()["id"]

    # Mark as paid and confirmed
    booking = db_session.query(models.Booking).filter_by(id=booking_id).first()
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED
    db_session.commit()

    response = client.post(
        f"/bookings/{booking_id}/extend-hold",
        json={"email": "athit@example.com"},
    )
    assert response.status_code == 409
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail["code"] == "DUPLICATE_BOOKING"
    assert "message" in detail
    assert "already been paid" in detail["message"]


# ─── Test 15: Verify all error codes are used ──────────────────────────────────

def test_all_error_codes_documented(client, room_id):
    """smoke test to verify various error codes can be triggered."""
    # Collect various error codes from different scenarios
    errors_found = set()

    # ROOM_NOT_FOUND
    resp1 = client.post("/bookings", json=booking_payload(999999))
    errors_found.add(resp1.json()["detail"]["code"])

    # INVALID_DATE_RANGE
    now = datetime.now(timezone.utc)
    resp2 = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=now.isoformat(),
            check_out=(now - timedelta(days=1)).isoformat(),
        ),
    )
    errors_found.add(resp2.json()["detail"]["code"])

    # CHECK_IN_PAST
    resp3 = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=(now - timedelta(days=1)).isoformat(),
            check_out=(now + timedelta(days=1)).isoformat(),
        ),
    )
    errors_found.add(resp3.json()["detail"]["code"])

    # GUEST_CAPACITY_EXCEEDED
    resp4 = client.post("/bookings", json=booking_payload(room_id, guests=99))
    errors_found.add(resp4.json()["detail"]["code"])

    # HOLD_NOT_FOUND
    resp5 = client.patch("/bookings/999999/cancel")
    errors_found.add(resp5.json()["detail"]["code"])

    # Verify we got various error codes
    assert "ROOM_NOT_FOUND" in errors_found
    assert "INVALID_DATE_RANGE" in errors_found
    assert "CHECK_IN_PAST" in errors_found
    assert "GUEST_CAPACITY_EXCEEDED" in errors_found
    assert "HOLD_NOT_FOUND" in errors_found
