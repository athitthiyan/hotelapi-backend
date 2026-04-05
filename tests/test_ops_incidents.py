from datetime import timedelta, timezone, datetime

import models
from routers.auth import hash_password


def admin_headers(client, db_session):
    admin = models.User(
        email="admin-ops@example.com",
        full_name="Admin Ops",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-ops@example.com", "password": "AdminPass123"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_incident_dashboard_lists_orphan_paid_processing_and_active_holds(
    client, create_booking, db_session
):
    headers = admin_headers(client, db_session)
    booking = create_booking()
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    booking_row.payment_status = models.PaymentStatus.PAID
    booking_row.status = models.BookingStatus.CONFIRMED

    processing = client.post(
        "/bookings",
        json={
            "user_name": "Processing User",
            "email": "processing@example.com",
            "phone": "1234567890",
            "room_id": booking["room_id"],
            "check_in": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            "check_out": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
            "guests": 1,
            "special_requests": "",
        },
    )
    assert processing.status_code == 201
    processing_payload = processing.json()
    processing_row = db_session.query(models.Booking).filter_by(id=processing_payload["id"]).first()
    processing_row.payment_status = models.PaymentStatus.PROCESSING
    processing_row.status = models.BookingStatus.PROCESSING

    held = client.post(
        "/bookings",
        json={
            "user_name": "Held User",
            "email": "held@example.com",
            "phone": "1234567890",
            "room_id": booking["room_id"],
            "check_in": (datetime.now(timezone.utc) + timedelta(days=6)).isoformat(),
            "check_out": (datetime.now(timezone.utc) + timedelta(days=8)).isoformat(),
            "guests": 1,
            "special_requests": "",
        },
    )
    assert held.status_code == 201
    held_payload = held.json()
    held_row = db_session.query(models.Booking).filter_by(id=held_payload["id"]).first()
    held_row.hold_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db_session.commit()

    response = client.get("/ops/incidents", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert any(item["booking_id"] == booking["id"] for item in body["orphan_paid_bookings"])
    assert any(item["booking_id"] == processing_payload["id"] for item in body["stale_processing_bookings"])
    assert any(item["booking_id"] == held_payload["id"] for item in body["active_holds"])


def test_release_hold_endpoint_cancels_unpaid_booking(client, create_booking, db_session):
    headers = admin_headers(client, db_session)
    booking = create_booking()

    response = client.post(f"/ops/bookings/{booking['id']}/release-hold", headers=headers)

    db_booking = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert response.status_code == 200
    assert db_booking.status == models.BookingStatus.CANCELLED


def test_force_confirm_endpoint_requires_success_transaction(client, create_booking, db_session):
    headers = admin_headers(client, db_session)
    booking = create_booking()

    response = client.post(f"/ops/bookings/{booking['id']}/force-confirm", headers=headers)

    assert response.status_code == 409
    assert "successful transaction" in response.json()["detail"]


def test_force_confirm_endpoint_confirms_paid_booking_with_success_transaction(
    client, create_booking, db_session
):
    headers = admin_headers(client, db_session)
    booking = create_booking()
    intent = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock", "idempotency_key": "ops-confirm-001"},
    )
    client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking["id"],
            "payment_intent_id": intent.json()["payment_intent_id"],
            "transaction_ref": intent.json()["transaction_ref"],
            "payment_method": "mock",
        },
    )
    db_booking = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    db_booking.status = models.BookingStatus.PROCESSING
    db_booking.payment_status = models.PaymentStatus.PROCESSING
    db_session.commit()

    response = client.post(f"/ops/bookings/{booking['id']}/force-confirm", headers=headers)

    db_session.refresh(db_booking)
    assert response.status_code == 200
    assert db_booking.status == models.BookingStatus.CONFIRMED
    assert db_booking.payment_status == models.PaymentStatus.PAID
