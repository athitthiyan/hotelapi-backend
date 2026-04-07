from routers.auth import hash_password

import models


def admin_headers(client, db_session):
    admin = models.User(
        email="admin-notify@example.com",
        full_name="Admin Notify",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-notify@example.com", "password": "AdminPass123"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_booking_creation_queues_hold_notification(client, create_booking, db_session):
    booking = create_booking()

    notifications = (
        db_session.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.booking_id == booking["id"])
        .all()
    )

    assert len(notifications) == 1
    assert notifications[0].event_type == "booking_hold_created"
    assert notifications[0].status == models.NotificationStatus.PENDING


def test_payment_success_queues_confirmation_and_receipt_notifications(
    client, create_booking, db_session
):
    booking = create_booking()
    intent = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock", "idempotency_key": "notify-mock-001"},
    )
    success = client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking["id"],
            "payment_intent_id": intent.json()["payment_intent_id"],
            "transaction_ref": intent.json()["transaction_ref"],
            "payment_method": "mock",
        },
    )

    notifications = (
        db_session.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.booking_id == booking["id"])
        .order_by(models.NotificationOutbox.id.asc())
        .all()
    )

    assert success.status_code == 200
    assert [n.event_type for n in notifications] == [
        "booking_hold_created",
        "booking_confirmed",
        "payment_receipt",
    ]

    # Confirmation email must have invoice PDF attached
    confirmation = [n for n in notifications if n.event_type == "booking_confirmed"][0]
    assert confirmation.attachment_pdf is not None
    assert confirmation.attachment_pdf[:5] == b"%PDF-"
    assert confirmation.attachment_filename is not None
    assert confirmation.attachment_filename.endswith(".pdf")

    # Hold and receipt emails must NOT have attachments
    hold = [n for n in notifications if n.event_type == "booking_hold_created"][0]
    receipt = [n for n in notifications if n.event_type == "payment_receipt"][0]
    assert hold.attachment_pdf is None
    assert receipt.attachment_pdf is None


def test_payment_failure_queues_retry_notification(client, create_booking, db_session):
    booking = create_booking()
    intent = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock", "idempotency_key": "notify-fail-001"},
    )
    failure = client.post(
        "/payments/payment-failure",
        params={
            "booking_id": booking["id"],
            "payment_intent_id": intent.json()["payment_intent_id"],
            "transaction_ref": intent.json()["transaction_ref"],
            "reason": "Card declined",
        },
    )

    notifications = (
        db_session.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.booking_id == booking["id"])
        .order_by(models.NotificationOutbox.id.asc())
        .all()
    )

    assert failure.status_code == 200
    assert notifications[-1].event_type == "payment_failed_retry"
    assert notifications[-1].status == models.NotificationStatus.PENDING


def test_booking_cancellation_queues_notification(client, create_booking, db_session):
    booking = create_booking()

    response = client.patch(f"/bookings/{booking['id']}/cancel")
    notifications = (
        db_session.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.booking_id == booking["id"])
        .order_by(models.NotificationOutbox.id.asc())
        .all()
    )

    assert response.status_code == 200
    assert notifications[-1].event_type == "booking_cancelled"


def test_refund_lifecycle_queues_initiated_success_and_failure_notifications(
    client, create_booking, db_session
):
    headers = admin_headers(client, db_session)
    booking = create_booking()
    intent = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock", "idempotency_key": "notify-refund-001"},
    )
    success = client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking["id"],
            "payment_intent_id": intent.json()["payment_intent_id"],
            "transaction_ref": intent.json()["transaction_ref"],
            "payment_method": "mock",
        },
    )

    initiated = client.post(
        "/payments/refund",
        headers=headers,
        json={"booking_id": booking["id"], "reason": "Refund initiated"},
    )
    failed = client.post(
        f"/payments/refunds/{booking['id']}/fail",
        headers=headers,
        json={"reason": "Gateway issue"},
    )
    completed = client.post(
        f"/payments/refunds/{booking['id']}/complete",
        headers=headers,
        json={"reason": "Refund settled", "gateway_reference": "RFND-NOTIFY-001"},
    )

    notifications = (
        db_session.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.booking_id == booking["id"])
        .order_by(models.NotificationOutbox.id.asc())
        .all()
    )

    assert success.status_code == 200
    assert initiated.status_code == 200
    assert failed.status_code == 200
    assert completed.status_code == 200
    assert [n.event_type for n in notifications][-3:] == [
        "refund_initiated",
        "refund_failed",
        "refund_success",
    ]


def test_notification_outbox_processing_sent_and_failed_paths(client, db_session):
    headers = admin_headers(client, db_session)
    room = models.Room(
        hotel_name="Notify Hotel",
        room_type=models.RoomType.DELUXE,
        price=200,
        availability=True,
        city="Test City",
        country="Test Country",
    )
    db_session.add(room)
    db_session.commit()
    db_session.refresh(room)

    valid_booking = client.post(
        "/bookings",
        json={
            "user_name": "Valid User",
            "email": "valid@example.com",
            "phone": "1234567890",
            "room_id": room.id,
            "check_in": "2026-04-10T00:00:00+00:00",
            "check_out": "2026-04-12T00:00:00+00:00",
            "guests": 2,
            "special_requests": "",
        },
    )
    invalid_booking = client.post(
        "/bookings",
        json={
            "user_name": "Invalid User",
            "email": "fail-delivery+broken@example.com",
            "phone": "1234567890",
            "room_id": room.id,
            "check_in": "2026-04-15T00:00:00+00:00",
            "check_out": "2026-04-17T00:00:00+00:00",
            "guests": 2,
            "special_requests": "",
        },
    )

    process = client.post("/notifications/process", headers=headers, params={"limit": 10})
    outbox = client.get("/notifications/outbox", headers=headers)

    assert valid_booking.status_code == 201
    assert invalid_booking.status_code == 201
    assert process.status_code == 200
    assert process.json()["total"] >= 2
    assert process.json()["sent"] >= 1
    assert process.json()["failed"] >= 1
    statuses = {item["recipient_email"]: item["status"] for item in outbox.json()["notifications"]}
    assert statuses["valid@example.com"] == "sent"
    assert statuses["fail-delivery+broken@example.com"] == "failed"
