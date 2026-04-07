from unittest.mock import patch

from routers.auth import hash_password

import models


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def webhook_headers() -> dict:
    return {"stripe-signature": "test-signature"}


def create_admin_headers(client, db_session) -> dict:
    admin = models.User(
        email="admin-e2e-retry@example.com",
        full_name="Admin Retry",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    login = client.post(
        "/auth/login",
        json={"email": "admin-e2e-retry@example.com", "password": "AdminPass123"},
    )
    assert login.status_code == 200
    return auth_header(login.json()["access_token"])


def send_webhook_success(client, booking_id: int, payment_intent_id: str, transaction_ref: str):
    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": payment_intent_id,
                "metadata": {
                    "booking_id": str(booking_id),
                    "transaction_ref": transaction_ref,
                },
                "charges": {
                    "data": [
                        {
                            "payment_method_details": {
                                "card": {"last4": "4242", "brand": "visa"}
                            }
                        }
                    ]
                },
            }
        },
    }
    with patch("routers.payments.stripe.Webhook.construct_event", return_value=event):
        return client.post("/payments/webhook", headers=webhook_headers(), content=b"{}")


def test_e2e_customer_booking_to_confirmation_flow(client, db_session):
    admin = models.User(
        email="admin-e2e@example.com",
        full_name="Admin E2E",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    admin_login = client.post(
        "/auth/login",
        json={"email": "admin-e2e@example.com", "password": "AdminPass123"},
    )
    assert admin_login.status_code == 200
    admin_headers = auth_header(admin_login.json()["access_token"])

    room_response = client.post(
        "/rooms",
        headers=admin_headers,
        json={
            "hotel_name": "E2E Hotel",
            "room_type": "suite",
            "description": "E2E room",
            "price": 250,
            "availability": True,
            "rating": 4.8,
            "review_count": 12,
            "image_url": "https://example.com/e2e.jpg",
            "gallery_urls": None,
            "amenities": None,
            "location": "Chennai",
            "city": "Chennai",
            "country": "India",
            "max_guests": 3,
            "beds": 2,
            "bathrooms": 1,
            "size_sqft": 550,
            "floor": 7,
            "is_featured": True,
        },
    )
    assert room_response.status_code == 201
    room_id = room_response.json()["id"]

    signup = client.post(
        "/auth/signup",
        json={
            "email": "customer-e2e@example.com",
            "full_name": "Customer E2E",
            "password": "CustomerPass123",
        },
    )
    assert signup.status_code == 201

    booking = client.post(
        "/bookings",
        json={
            "user_name": "Customer E2E",
            "email": "customer-e2e@example.com",
            "phone": "9876543210",
            "room_id": room_id,
            "check_in": "2026-04-20T00:00:00+00:00",
            "check_out": "2026-04-23T00:00:00+00:00",
            "guests": 2,
            "special_requests": "Late check-in",
        },
    )
    assert booking.status_code == 201
    booking_body = booking.json()

    payment_intent = client.post(
        "/payments/create-payment-intent",
        json={
            "booking_id": booking_body["id"],
            "payment_method": "mock",
            "idempotency_key": "e2e-booking-payment-001",
        },
    )
    assert payment_intent.status_code == 200

    payment_success = client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking_body["id"],
            "payment_intent_id": payment_intent.json()["payment_intent_id"],
            "transaction_ref": payment_intent.json()["transaction_ref"],
            "payment_method": "mock",
        },
    )
    assert payment_success.status_code == 200

    booking_by_ref = client.get(f"/bookings/ref/{booking_body['booking_ref']}")
    payment_status = client.get(f"/payments/status/{booking_body['id']}")
    admin_outbox = client.get("/notifications/outbox", headers=admin_headers)
    process_notifications = client.post("/notifications/process", headers=admin_headers)
    transactions = client.get("/payments/transactions", headers=admin_headers)

    assert booking_by_ref.status_code == 200
    assert booking_by_ref.json()["status"] == "confirmed"
    assert payment_status.status_code == 200
    assert payment_status.json()["payment_status"] == "paid"
    assert payment_status.json()["latest_transaction"]["status"] == "success"
    assert transactions.status_code == 200
    assert transactions.json()["total"] >= 1
    assert admin_outbox.status_code == 200
    assert admin_outbox.json()["total"] >= 3
    event_types = {item["event_type"] for item in admin_outbox.json()["notifications"]}
    assert "booking_hold_created" in event_types
    assert "booking_confirmed" in event_types
    assert "payment_receipt" in event_types
    assert process_notifications.status_code == 200
    assert process_notifications.json()["total"] >= 3


def test_e2e_card_decline_then_retry_success_flow(client, create_booking, db_session):
    booking = create_booking()
    admin_headers = create_admin_headers(client, db_session)

    def stripe_intent(intent_id: str, secret: str):
        class Intent:
            id = intent_id
            client_secret = secret

        return Intent()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        side_effect=[
            stripe_intent("pi_e2e_fail_001", "secret_fail_001"),
            stripe_intent("pi_e2e_retry_001", "secret_retry_001"),
        ],
    ):
        first_intent = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking["id"],
                "payment_method": "card",
                "idempotency_key": "e2e-retry-001",
            },
        )
        assert first_intent.status_code == 200

        failed = client.post(
            "/payments/payment-failure",
            params={
                "booking_id": booking["id"],
                "payment_intent_id": first_intent.json()["payment_intent_id"],
                "transaction_ref": first_intent.json()["transaction_ref"],
                "reason": "Card declined",
            },
        )
        assert failed.status_code == 200

        second_intent = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking["id"],
                "payment_method": "card",
                "idempotency_key": "e2e-retry-002",
            },
        )
        assert second_intent.status_code == 200

    success = client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking["id"],
            "payment_intent_id": second_intent.json()["payment_intent_id"],
            "transaction_ref": second_intent.json()["transaction_ref"],
            "payment_method": "card",
            "card_last4": "4242",
            "card_brand": "visa",
        },
    )
    assert success.status_code == 200

    webhook = send_webhook_success(
        client,
        booking["id"],
        second_intent.json()["payment_intent_id"],
        second_intent.json()["transaction_ref"],
    )
    assert webhook.status_code == 200

    payment_status = client.get(f"/payments/status/{booking['id']}")
    booking_by_ref = client.get(f"/bookings/ref/{booking['booking_ref']}")
    transactions = client.get("/payments/transactions", headers=admin_headers)

    assert payment_status.status_code == 200
    assert payment_status.json()["payment_status"] == "paid"
    assert payment_status.json()["latest_transaction"]["status"] == "success"

    assert booking_by_ref.status_code == 200
    assert booking_by_ref.json()["status"] == "confirmed"
    assert booking_by_ref.json()["payment_status"] == "paid"

    assert transactions.status_code == 200
    txn_refs = {txn["transaction_ref"] for txn in transactions.json()["transactions"]}
    assert first_intent.json()["transaction_ref"] in txn_refs
    assert second_intent.json()["transaction_ref"] in txn_refs

    db_txns = (
        db_session.query(models.Transaction)
        .filter(models.Transaction.booking_id == booking["id"])
        .order_by(models.Transaction.id.asc())
        .all()
    )
    assert len(db_txns) == 2
    assert db_txns[0].status == models.TransactionStatus.FAILED
    assert db_txns[1].status == models.TransactionStatus.SUCCESS
    assert db_txns[1].retry_of_transaction_id == db_txns[0].id
