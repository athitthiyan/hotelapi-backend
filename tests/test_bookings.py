from datetime import datetime, timedelta, timezone

import models
from routers.auth import hash_password


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


def admin_headers(client, db_session):
    admin = models.User(
        email="admin-bookings@example.com",
        full_name="Admin Bookings",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-bookings@example.com", "password": "AdminPass123"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_create_booking_room_not_found(client):
    response = client.post("/bookings", json=booking_payload(999999))
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "ROOM_NOT_FOUND"
    assert "Room not found" in detail["message"]


def test_create_booking_room_unavailable(client, db_session, room_id):
    room = db_session.query(__import__("models").Room).filter_by(id=room_id).first()
    room.availability = False
    db_session.commit()

    response = client.post("/bookings", json=booking_payload(room_id))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "ROOM_UNAVAILABLE"
    assert "not currently available" in detail["message"]


def test_create_booking_invalid_checkout_before_checkin(client, room_id):
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
    detail = response.json()["detail"]
    assert detail["code"] == "INVALID_DATE_RANGE"
    assert "after check-in" in detail["message"]


def test_create_booking_minimum_stay_validation(client, room_id):
    now = datetime.now(timezone.utc)
    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=now.isoformat(),
            check_out=now.isoformat(),
        ),
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "INVALID_DATE_RANGE"
    assert "after check-in" in detail["message"]


def test_get_bookings_filters_by_email_and_status(client, create_booking, db_session):
    first = create_booking()
    second_response = client.post(
        "/bookings",
        json={
            **booking_payload(
                first["room_id"],
                check_in=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
                check_out=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            ),
            "email": "other@example.com",
        },
    )
    assert second_response.status_code == 201

    booking = db_session.query(__import__("models").Booking).filter_by(id=first["id"]).first()
    booking.status = __import__("models").BookingStatus.CONFIRMED
    db_session.commit()

    response = client.get("/bookings", params={"email": "athit@example.com", "status": "confirmed"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["bookings"][0]["email"] == "athit@example.com"
    assert body["bookings"][0]["status"] == "confirmed"


def test_get_booking_history_and_by_id_and_ref(client, create_booking):
    booking = create_booking()

    history = client.get("/bookings/history", params={"email": "athit@example.com"})
    by_id = client.get(f"/bookings/{booking['id']}")
    by_ref = client.get(f"/bookings/ref/{booking['booking_ref']}")

    assert history.status_code == 200
    assert history.json()["total"] == 1
    assert by_id.status_code == 200
    assert by_ref.status_code == 200
    assert by_ref.json()["booking_ref"] == booking["booking_ref"]


def test_get_booking_not_found_and_ref_not_found(client):
    by_id = client.get("/bookings/999999")
    by_ref = client.get("/bookings/ref/BKNOTFOUND")

    assert by_id.status_code == 404
    assert by_ref.status_code == 404


def test_cancel_booking_success_and_already_cancelled_and_not_found(client, create_booking):
    booking = create_booking()

    first = client.patch(f"/bookings/{booking['id']}/cancel")
    second = client.patch(f"/bookings/{booking['id']}/cancel")
    missing = client.patch("/bookings/999999/cancel")

    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert second.status_code == 400
    detail_second = second.json()["detail"]
    assert detail_second["code"] == "HOLD_EXPIRED"
    assert "already been cancelled" in detail_second["message"]
    assert missing.status_code == 404
    detail_missing = missing.json()["detail"]
    assert detail_missing["code"] == "HOLD_NOT_FOUND"


def test_create_booking_sets_hold_expiry(client, room_id):
    response = client.post("/bookings", json=booking_payload(room_id))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["hold_expires_at"] is not None


def test_create_booking_locks_inventory_for_each_stay_date(client, db_session, room_id):
    response = client.post("/bookings", json=booking_payload(room_id))
    booking = db_session.query(models.Booking).filter_by(id=response.json()["id"]).first()

    inventory_rows = (
        db_session.query(models.RoomInventory)
        .filter(models.RoomInventory.locked_by_booking_id == booking.id)
        .order_by(models.RoomInventory.inventory_date.asc())
        .all()
    )

    assert response.status_code == 201
    assert len(inventory_rows) == 2
    assert all(row.locked_units == 1 for row in inventory_rows)


def test_create_booking_blocks_overlapping_active_reservations(client, create_booking, room_id):
    first = create_booking()

    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=first["check_in"],
            check_out=first["check_out"],
        ),
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "HOLD_EXISTS"
    assert "active booking hold" in detail["message"]


def test_expired_booking_hold_is_released_for_new_reservation(client, create_booking, db_session, room_id):
    first = create_booking()
    booking = db_session.query(models.Booking).filter_by(id=first["id"]).first()
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=first["check_in"],
            check_out=first["check_out"],
        ),
    )

    db_session.refresh(booking)
    assert response.status_code == 201
    assert booking.status == models.BookingStatus.EXPIRED
    assert booking.payment_status == models.PaymentStatus.EXPIRED


def test_get_booking_by_ref_expires_stale_holds(client, create_booking, db_session):
    created = create_booking()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    response = client.get(f"/bookings/ref/{created['booking_ref']}")

    assert response.status_code == 200
    assert response.json()["status"] == "expired"


def test_cancel_paid_booking_requires_refund_workflow(client, create_booking, db_session):
    created = create_booking()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED
    db_session.commit()

    response = client.patch(f"/bookings/{created['id']}/cancel")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "PAYMENT_FAILED"
    assert "refund workflow" in detail["message"]


def test_cancelling_booking_releases_inventory_lock(client, create_booking, db_session):
    created = create_booking()
    response = client.patch(f"/bookings/{created['id']}/cancel")
    inventory_rows = (
        db_session.query(models.RoomInventory)
        .filter(models.RoomInventory.room_id == created["room_id"])
        .all()
    )

    assert response.status_code == 200
    assert inventory_rows
    assert all(row.locked_units == 0 for row in inventory_rows)
    assert all(row.locked_by_booking_id is None for row in inventory_rows)


def test_admin_booking_dashboard_filters_and_counts(client, create_booking, db_session):
    headers = admin_headers(client, db_session)
    first = create_booking()
    second = client.post(
        "/bookings",
        json=booking_payload(
            first["room_id"],
            email="other@example.com",
            check_in=(datetime.now(timezone.utc) + timedelta(days=4)).isoformat(),
            check_out=(datetime.now(timezone.utc) + timedelta(days=6)).isoformat(),
        ),
    )
    first_row = db_session.query(models.Booking).filter_by(id=first["id"]).first()
    second_row = db_session.query(models.Booking).filter_by(id=second.json()["id"]).first()
    first_row.status = models.BookingStatus.CONFIRMED
    first_row.payment_status = models.PaymentStatus.PAID
    second_row.payment_status = models.PaymentStatus.FAILED
    db_session.commit()

    response = client.get(
        "/bookings/admin/dashboard",
        headers=headers,
        params={"payment_status": "failed"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["bookings"][0]["email"] == "other@example.com"
    assert body["pending_count"] >= 0
    assert body["confirmed_count"] >= 1
    assert body["failed_payment_count"] >= 1


def test_booking_creation_rejects_invalid_phone_number(client, room_id):
    response = client.post(
        "/bookings",
        json={
            "user_name": "Athit",
            "email": "athit@example.com",
            "phone": "abc-not-valid",
            "room_id": room_id,
            "check_in": datetime.now(timezone.utc).isoformat(),
            "check_out": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "guests": 2,
            "special_requests": "",
        },
    )

    assert response.status_code == 422


# ─── Resumable booking ────────────────────────────────────────────────────────

def test_resumable_booking_found(client, room_id):
    """A PENDING booking with a non-expired hold should be returned."""
    created = client.post("/bookings", json=booking_payload(room_id)).json()
    params = {
        "room_id": room_id,
        "check_in": created["check_in"],
        "check_out": created["check_out"],
        "email": "athit@example.com",
    }
    response = client.get("/bookings/resumable", params=params)
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert response.json()["booking_ref"] == created["booking_ref"]


def test_resumable_booking_not_found_wrong_email(client, room_id):
    """A different email should not return a resumable booking."""
    created = client.post("/bookings", json=booking_payload(room_id)).json()
    params = {
        "room_id": room_id,
        "check_in": created["check_in"],
        "check_out": created["check_out"],
        "email": "stranger@example.com",
    }
    response = client.get("/bookings/resumable", params=params)
    assert response.status_code == 404


def test_resumable_booking_not_found_no_booking(client, room_id):
    """No booking exists → 404."""
    now = datetime.now(timezone.utc)
    params = {
        "room_id": room_id,
        "check_in": now.isoformat(),
        "check_out": (now + timedelta(days=2)).isoformat(),
        "email": "nobody@example.com",
    }
    response = client.get("/bookings/resumable", params=params)
    assert response.status_code == 404


def test_resumable_booking_expired_hold_not_returned(client, db_session, room_id):
    """An expired hold must NOT be returned as resumable."""
    created = client.post("/bookings", json=booking_payload(room_id)).json()

    # Manually expire the hold
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    params = {
        "room_id": room_id,
        "check_in": created["check_in"],
        "check_out": created["check_out"],
        "email": "athit@example.com",
    }
    response = client.get("/bookings/resumable", params=params)
    assert response.status_code == 404


# ─── Extend hold ─────────────────────────────────────────────────────────────

def test_extend_hold_success(client, db_session, room_id):
    """Successfully extend a hold after it expired when dates are still free."""
    created = client.post("/bookings", json=booking_payload(room_id)).json()

    # Expire the hold manually
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    booking.status = models.BookingStatus.EXPIRED
    booking.payment_status = models.PaymentStatus.EXPIRED
    db_session.commit()

    # Release inventory locks (simulates what the cron/cleanup would do)
    from services.inventory_service import release_inventory_for_booking
    release_inventory_for_booking(db_session, booking=booking)
    db_session.commit()

    response = client.post(
        f"/bookings/{created['id']}/extend-hold",
        json={"email": "athit@example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["payment_status"] == "pending"
    assert body["hold_expires_at"] is not None
    # New expiry should be in the future (handle both naive and aware ISO strings)
    from datetime import datetime as _dt
    raw = body["hold_expires_at"].replace("Z", "+00:00")
    new_exp = _dt.fromisoformat(raw)
    if new_exp.tzinfo is None:
        new_exp = new_exp.replace(tzinfo=timezone.utc)
    assert new_exp > datetime.now(timezone.utc)


def test_extend_hold_email_mismatch(client, room_id):
    """Wrong email → 403."""
    created = client.post("/bookings", json=booking_payload(room_id)).json()
    response = client.post(
        f"/bookings/{created['id']}/extend-hold",
        json={"email": "wrong@example.com"},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "AUTH_REQUIRED"
    assert "Email" in detail["message"]


def test_extend_hold_already_paid(client, db_session, room_id):
    """A paid booking cannot have its hold extended."""
    created = client.post("/bookings", json=booking_payload(room_id)).json()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED
    db_session.commit()

    response = client.post(
        f"/bookings/{created['id']}/extend-hold",
        json={"email": "athit@example.com"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "DUPLICATE_BOOKING"
    assert "already been paid" in detail["message"]


def test_extend_hold_dates_taken_by_confirmed_booking(client, db_session, room_id):
    """If another confirmed booking grabbed the dates, extend-hold returns 409."""
    now = datetime.now(timezone.utc)
    check_in = now.isoformat()
    check_out = (now + timedelta(days=2)).isoformat()

    # Create original booking then expire its hold
    original = client.post("/bookings", json=booking_payload(room_id)).json()
    booking = db_session.query(models.Booking).filter_by(id=original["id"]).first()
    booking.hold_expires_at = now - timedelta(minutes=1)
    booking.status = models.BookingStatus.EXPIRED
    db_session.commit()

    from services.inventory_service import release_inventory_for_booking
    release_inventory_for_booking(db_session, booking=booking)
    db_session.commit()

    # Second booking grabs the same dates and gets confirmed
    other_payload = {
        **booking_payload(room_id, check_in=check_in, check_out=check_out),
        "email": "other@example.com",
    }
    other = client.post("/bookings", json=other_payload).json()
    assert other.get("id"), f"Second booking failed: {other}"
    other_booking = db_session.query(models.Booking).filter_by(id=other["id"]).first()
    other_booking.status = models.BookingStatus.CONFIRMED
    db_session.commit()

    # Attempt to extend-hold the original booking → should fail
    response = client.post(
        f"/bookings/{original['id']}/extend-hold",
        json={"email": "athit@example.com"},
    )
    assert response.status_code == 409


# ─── Race condition ───────────────────────────────────────────────────────────

def test_race_condition_two_simultaneous_bookings(client, room_id):
    """Only one of two simultaneous booking attempts for the same dates should succeed."""
    now = datetime.now(timezone.utc)
    payload_a = booking_payload(
        room_id,
        check_in=now.isoformat(),
        check_out=(now + timedelta(days=2)).isoformat(),
        email="user_a@example.com",
    )
    payload_b = {
        **booking_payload(
            room_id,
            check_in=now.isoformat(),
            check_out=(now + timedelta(days=2)).isoformat(),
        ),
        "email": "user_b@example.com",
    }

    resp_a = client.post("/bookings", json=payload_a)
    resp_b = client.post("/bookings", json=payload_b)

    statuses = {resp_a.status_code, resp_b.status_code}
    # One must succeed (201) and one must fail (409)
    assert 201 in statuses, "At least one booking should succeed"
    assert 409 in statuses, "At least one booking should be rejected as a conflict"
