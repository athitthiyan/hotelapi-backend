from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import models
from routers.auth import hash_password


def booking_payload(room_id: int, **overrides):
    payload = {
        "user_name": "Athit",
        "email": "athit@example.com",
        "phone": "1234567890",
        "room_id": room_id,
        "check_in": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "check_out": (datetime.now(timezone.utc) + timedelta(days=2, hours=2)).isoformat(),
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


def user_headers(client, db_session, email="athit@example.com", password="UserPass123"):
    user = models.User(
        email=email,
        full_name="Auth User",
        hashed_password=hash_password(password),
        is_admin=False,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def stripe_intent(intent_id: str, client_secret: str):
    return MagicMock(id=intent_id, client_secret=client_secret)


def stripe_retrieved_intent(intent_id: str, status: str, last4: str = "4242", brand: str = "visa"):
    return {
        "id": intent_id,
        "status": status,
        "charges": {
            "data": [
                {
                    "payment_method_details": {
                        "card": {"last4": last4, "brand": brand}
                    }
                }
            ]
        },
    }


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


def test_guest_booking_endpoints_reconcile_processing_card_payment_and_remove_active_hold(
    client,
    db_session,
    room_id,
):
    headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id), headers=headers)
    assert created.status_code == 201
    booking_id = created.json()["id"]

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_booking_reconcile_001", "secret_booking_reconcile_001"),
    ), patch(
        "routers.payments.stripe.PaymentIntent.retrieve",
        return_value=stripe_retrieved_intent("pi_booking_reconcile_001", "processing"),
    ):
        intent = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking_id,
                "payment_method": "card",
                "idempotency_key": "booking-reconcile-001",
            },
        )
        acknowledged = client.post(
            "/payments/payment-success",
            json={
                "booking_id": booking_id,
                "payment_intent_id": intent.json()["payment_intent_id"],
                "transaction_ref": intent.json()["transaction_ref"],
                "payment_method": "card",
            },
        )

    assert acknowledged.status_code == 200
    assert acknowledged.json()["status"] == "processing"

    with patch(
        "services.payment_state_service.stripe.PaymentIntent.retrieve",
        return_value=stripe_retrieved_intent("pi_booking_reconcile_001", "succeeded"),
    ):
        by_id = client.get(f"/bookings/{booking_id}")
        history = client.get("/bookings/history", params={"email": "athit@example.com"})
        my_bookings = client.get("/auth/me/bookings", headers=headers)
        active_hold = client.get("/bookings/active-hold", headers=headers)

    assert by_id.status_code == 200
    assert by_id.json()["payment_status"] == "paid"
    assert by_id.json()["status"] == "confirmed"
    assert by_id.json()["lifecycle_state"] == "CONFIRMED"
    assert history.status_code == 200
    assert history.json()["bookings"][0]["lifecycle_state"] == "CONFIRMED"
    assert my_bookings.status_code == 200
    assert my_bookings.json()["bookings"][0]["lifecycle_state"] == "CONFIRMED"
    assert active_hold.status_code == 204


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


def test_create_booking_blocks_multiple_active_holds_for_same_logged_in_user(client, db_session, room_id):
    headers = user_headers(client, db_session)
    client.post("/bookings", json=booking_payload(room_id), headers=headers)

    response = client.post(
        "/bookings",
        json=booking_payload(
            room_id,
            check_in=(datetime.now(timezone.utc) + timedelta(days=4)).isoformat(),
            check_out=(datetime.now(timezone.utc) + timedelta(days=6)).isoformat(),
        ),
        headers=headers,
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "HOLD_EXISTS"
    assert "existing reservation" in detail["message"]

    active_hold = client.get("/bookings/active-hold", headers=headers)
    assert active_hold.status_code == 200
    assert active_hold.json()["booking_id"] > 0


def test_create_booking_uses_authenticated_user_for_active_hold_lookup_when_email_differs(
    client,
    db_session,
    room_id,
):
    headers = user_headers(client, db_session, email="owner@example.com")

    create_response = client.post(
        "/bookings",
        json=booking_payload(room_id, email="guest.alias@example.com"),
        headers=headers,
    )

    assert create_response.status_code == 201
    created_booking = db_session.query(models.Booking).filter_by(id=create_response.json()["id"]).first()
    assert created_booking is not None
    assert created_booking.user_id is not None
    assert created_booking.email == "guest.alias@example.com"
    assert created_booking.user.email == "owner@example.com"

    active_hold = client.get("/bookings/active-hold", headers=headers)
    assert active_hold.status_code == 200
    assert active_hold.json()["booking_id"] == created_booking.id


def test_create_booking_guest_flow_preserves_unlinked_booking_when_email_has_no_user(
    client,
    db_session,
    room_id,
):
    response = client.post(
        "/bookings",
        json=booking_payload(room_id, email="guest.only@example.com"),
    )

    assert response.status_code == 201
    created_booking = db_session.query(models.Booking).filter_by(id=response.json()["id"]).first()
    assert created_booking is not None
    assert created_booking.user_id is None


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


def test_get_booking_by_ref_reconciles_processing_booking_with_success_transaction(
    client,
    create_booking,
    db_session,
):
    created = create_booking()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.status = models.BookingStatus.PROCESSING
    booking.payment_status = models.PaymentStatus.PROCESSING
    db_session.add(
        models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-BYREF-RECONCILE",
            amount=booking.total_amount,
            currency="USD",
            payment_method="card",
            status=models.TransactionStatus.SUCCESS,
        )
    )
    db_session.commit()

    response = client.get(f"/bookings/ref/{created['booking_ref']}")

    db_session.refresh(booking)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["payment_status"] == "paid"
    assert booking.status == models.BookingStatus.CONFIRMED
    assert booking.payment_status == models.PaymentStatus.PAID


def test_get_active_hold_returns_current_logged_in_hold(client, db_session, room_id):
    headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id)).json()

    response = client.get("/bookings/active-hold", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["booking_id"] == created["id"]
    assert body["room_id"] == room_id
    assert body["hotel_name"] == "Test Hotel"
    assert body["room_name"] == "deluxe"
    assert body["remaining_seconds"] > 0


def test_get_active_hold_excludes_expired_and_cancelled_and_confirmed(client, db_session, room_id):
    headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id)).json()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()

    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    expired = client.get("/bookings/active-hold", headers=headers)
    assert expired.status_code == 204

    booking.hold_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    booking.status = models.BookingStatus.CANCELLED
    db_session.commit()
    cancelled = client.get("/bookings/active-hold", headers=headers)
    assert cancelled.status_code == 204

    booking.status = models.BookingStatus.CONFIRMED
    booking.payment_status = models.PaymentStatus.PAID
    db_session.commit()
    confirmed = client.get("/bookings/active-hold", headers=headers)
    assert confirmed.status_code == 204


def test_get_active_hold_includes_processing_payment_holds(client, db_session, room_id):
    headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id)).json()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.status = models.BookingStatus.PROCESSING
    booking.payment_status = models.PaymentStatus.PROCESSING
    booking.hold_expires_at = datetime.now(timezone.utc) + timedelta(minutes=4)
    db_session.commit()

    response = client.get("/bookings/active-hold", headers=headers)

    assert response.status_code == 200
    assert response.json()["booking_id"] == created["id"]


def test_get_active_hold_reconciles_successful_processing_booking(client, db_session, room_id):
    headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id)).json()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.status = models.BookingStatus.PROCESSING
    booking.payment_status = models.PaymentStatus.PROCESSING
    db_session.add(
        models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-HOLD-RECONCILE",
            amount=booking.total_amount,
            currency="USD",
            payment_method="card",
            status=models.TransactionStatus.SUCCESS,
        )
    )
    db_session.commit()

    response = client.get("/bookings/active-hold", headers=headers)

    db_session.refresh(booking)
    assert response.status_code == 204
    assert booking.status == models.BookingStatus.CONFIRMED
    assert booking.payment_status == models.PaymentStatus.PAID


def test_get_active_hold_requires_authentication(client):
    response = client.get("/bookings/active-hold")
    assert response.status_code == 401


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
            "check_in": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "check_out": (datetime.now(timezone.utc) + timedelta(days=2, hours=2)).isoformat(),
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
    check_in = (now + timedelta(hours=2)).isoformat()
    check_out = (now + timedelta(days=2, hours=2)).isoformat()

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
        check_in=(now + timedelta(hours=2)).isoformat(),
        check_out=(now + timedelta(days=2, hours=2)).isoformat(),
        email="user_a@example.com",
    )
    payload_b = {
        **booking_payload(
            room_id,
            check_in=(now + timedelta(hours=2)).isoformat(),
            check_out=(now + timedelta(days=2, hours=2)).isoformat(),
        ),
        "email": "user_b@example.com",
    }

    resp_a = client.post("/bookings", json=payload_a)
    resp_b = client.post("/bookings", json=payload_b)

    statuses = {resp_a.status_code, resp_b.status_code}
    # One must succeed (201) and one must fail (409)
    assert 201 in statuses, "At least one booking should succeed"
    assert 409 in statuses, "At least one booking should be rejected as a conflict"


def test_support_request_queues_notification_for_booking_owner(client, db_session, room_id):
    headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id), headers=headers).json()

    response = client.post(
        f"/bookings/{created['id']}/support-request",
        headers=headers,
        json={
            "category": "cancellation_help",
            "message": "Please help me understand the refund amount before cancelling.",
        },
    )

    notification = (
        db_session.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.booking_id == created["id"])
        .order_by(models.NotificationOutbox.id.desc())
        .first()
    )
    assert response.status_code == 200
    assert "Support request submitted" in response.json()["message"]
    assert notification is not None
    assert notification.event_type == "booking_support_request"
    assert created["booking_ref"] in notification.subject


def test_support_request_requires_booking_owner(client, db_session, room_id):
    owner_headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id), headers=owner_headers).json()
    other_headers = user_headers(
        client,
        db_session,
        email="other-user@example.com",
        password="OtherPass123",
    )

    response = client.post(
        f"/bookings/{created['id']}/support-request",
        headers=other_headers,
        json={
            "category": "refund_help",
            "message": "I should not be able to request support for another booking.",
        },
    )

    assert response.status_code == 404


def test_invoice_and_voucher_download_for_booking_owner(client, db_session, room_id):
    headers = user_headers(client, db_session)
    created = client.post("/bookings", json=booking_payload(room_id), headers=headers).json()
    booking = db_session.query(models.Booking).filter_by(id=created["id"]).first()
    booking.status = models.BookingStatus.CONFIRMED
    booking.payment_status = models.PaymentStatus.PAID
    db_session.commit()

    invoice = client.get(f"/bookings/{created['id']}/invoice", headers=headers)
    voucher = client.get(f"/bookings/{created['id']}/voucher", headers=headers)

    assert invoice.status_code == 200
    assert invoice.headers["content-type"].startswith("application/pdf")
    assert b"Stayvora Tax Invoice" in invoice.content
    assert created["booking_ref"].encode() in invoice.content
    assert voucher.status_code == 200
    assert voucher.headers["content-type"].startswith("application/pdf")
    assert b"Stayvora Booking Voucher" in voucher.content


def test_invoice_document_requires_access_or_matching_reference(client, db_session, room_id):
    created = client.post("/bookings", json=booking_payload(room_id)).json()

    forbidden = client.get(f"/bookings/{created['id']}/invoice")
    by_reference = client.get(
        f"/bookings/{created['id']}/invoice",
        params={"booking_ref": created["booking_ref"]},
    )

    assert forbidden.status_code == 403
    assert by_reference.status_code == 200
