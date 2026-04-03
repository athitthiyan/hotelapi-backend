from datetime import timedelta
from unittest.mock import MagicMock, patch

import models
from routers.auth import hash_password


def acknowledge_card_payment(
    client, booking_id: int, ref: str, payment_intent_id: str | None = None
):
    return client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking_id,
            "payment_intent_id": payment_intent_id,
            "transaction_ref": ref,
            "payment_method": "card",
            "card_last4": "4242",
            "card_brand": "visa",
        },
    )


def confirm_mock_payment(
    client, booking_id: int, ref: str, payment_intent_id: str | None = None
):
    return client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking_id,
            "payment_intent_id": payment_intent_id,
            "transaction_ref": ref,
            "payment_method": "mock",
        },
    )


def stripe_intent(intent_id: str, client_secret: str):
    return MagicMock(id=intent_id, client_secret=client_secret)


def admin_headers(client, db_session):
    admin = models.User(
        email="admin-payments@example.com",
        full_name="Admin Payments",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-payments@example.com", "password": "AdminPass123"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_create_payment_intent_is_idempotent_for_same_key(client, create_booking, db_session):
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_same_001", "secret_same_001"),
    ) as create_intent:
        first = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking["id"],
                "payment_method": "card",
                "idempotency_key": "idem-001",
            },
        )
        second = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking["id"],
                "payment_method": "card",
                "idempotency_key": "idem-001",
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["payment_intent_id"] == "pi_same_001"
    assert second.json()["payment_intent_id"] == "pi_same_001"
    assert create_intent.call_count == 1
    assert db_session.query(models.Transaction).count() == 1


def test_duplicate_create_payment_intent_is_blocked_while_attempt_in_progress(client, create_booking):
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_dup_001", "secret_dup_001"),
    ):
        first = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "idem-key-a"},
        )
        second = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "idem-key-b"},
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "A payment attempt is already in progress for this booking"


def test_mock_payment_marks_booking_paid_immediately(client, create_booking, db_session):
    booking = create_booking()

    intent = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock", "idempotency_key": "mock-001"},
    )
    success = confirm_mock_payment(
        client,
        booking["id"],
        intent.json()["transaction_ref"],
        intent.json()["payment_intent_id"],
    )

    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert intent.status_code == 200
    assert success.status_code == 200
    assert success.json()["status"] == "success"
    assert booking_row.payment_status == models.PaymentStatus.PAID
    assert booking_row.status == models.BookingStatus.CONFIRMED


def test_card_payment_waits_for_webhook_confirmation(client, create_booking, db_session):
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_webhook_wait_001", "secret_wait_001"),
    ):
        intent = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "card-001"},
        )

    ack = acknowledge_card_payment(
        client,
        booking["id"],
        intent.json()["transaction_ref"],
        intent.json()["payment_intent_id"],
    )

    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert intent.status_code == 200
    assert ack.status_code == 200
    assert ack.json()["status"] == "processing"
    assert booking_row.payment_status == models.PaymentStatus.PROCESSING
    assert booking_row.status == models.BookingStatus.PROCESSING


def test_webhook_success_is_idempotent_and_finalizes_processing_payment(client, create_booking, db_session):
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_webhook_success_001", "secret_webhook_success_001"),
    ):
        intent = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "webhook-001"},
        )

    acknowledge_card_payment(
        client,
        booking["id"],
        intent.json()["transaction_ref"],
        intent.json()["payment_intent_id"],
    )

    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_webhook_success_001",
                "metadata": {
                    "booking_id": str(booking["id"]),
                    "transaction_ref": intent.json()["transaction_ref"],
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
        first = client.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig"})
        second = client.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig"})

    txns = db_session.query(models.Transaction).filter_by(booking_id=booking["id"]).all()
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(txns) == 1
    assert txns[0].status == models.TransactionStatus.SUCCESS
    assert booking_row.payment_status == models.PaymentStatus.PAID
    assert booking_row.status == models.BookingStatus.CONFIRMED


def test_failed_payment_can_retry_and_then_succeed(client, create_booking, db_session):
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        side_effect=[
            stripe_intent("pi_retry_fail_001", "secret_retry_fail_001"),
            stripe_intent("pi_retry_success_001", "secret_retry_success_001"),
        ],
    ):
        first_intent = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "retry-001"},
        )
        failure = client.post(
            "/payments/payment-failure",
            params={
                "booking_id": booking["id"],
                "payment_intent_id": first_intent.json()["payment_intent_id"],
                "transaction_ref": first_intent.json()["transaction_ref"],
                "reason": "Card declined",
            },
        )
        second_intent = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "retry-002"},
        )

    acknowledge_card_payment(
        client,
        booking["id"],
        second_intent.json()["transaction_ref"],
        second_intent.json()["payment_intent_id"],
    )

    success_event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_retry_success_001",
                "metadata": {
                    "booking_id": str(booking["id"]),
                    "transaction_ref": second_intent.json()["transaction_ref"],
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

    with patch("routers.payments.stripe.Webhook.construct_event", return_value=success_event):
        webhook = client.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig"})

    txns = (
        db_session.query(models.Transaction)
        .filter(models.Transaction.booking_id == booking["id"])
        .order_by(models.Transaction.id.asc())
        .all()
    )
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert first_intent.status_code == 200
    assert failure.status_code == 200
    assert second_intent.status_code == 200
    assert webhook.status_code == 200
    assert len(txns) == 2
    assert txns[0].status == models.TransactionStatus.FAILED
    assert txns[1].status == models.TransactionStatus.SUCCESS
    assert txns[1].retry_of_transaction_id == txns[0].id
    assert booking_row.payment_status == models.PaymentStatus.PAID


def test_payment_failure_rejected_when_booking_is_already_paid(client, create_booking):
    booking = create_booking()
    confirm_mock_payment(client, booking["id"], "TXN-LOCK001", "pi_lock_001")

    fail = client.post(
        "/payments/payment-failure",
        params={"booking_id": booking["id"], "reason": "Should not record"},
    )
    assert fail.status_code == 409


def test_payment_status_returns_latest_transaction(client, create_booking):
    booking = create_booking()
    intent = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock", "idempotency_key": "status-001"},
    )
    client.post(
        "/payments/payment-failure",
        params={
            "booking_id": booking["id"],
            "payment_intent_id": intent.json()["payment_intent_id"],
            "transaction_ref": intent.json()["transaction_ref"],
            "reason": "Retry later",
        },
    )

    response = client.get(f"/payments/status/{booking['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["booking_id"] == booking["id"]
    assert body["payment_status"] == "failed"
    assert body["latest_transaction"]["status"] == "failed"


def test_admin_refund_flow_and_reconciliation_dashboard(client, create_booking, db_session):
    headers = admin_headers(client, db_session)
    booking = create_booking()
    intent = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock", "idempotency_key": "refund-001"},
    )
    success = confirm_mock_payment(
        client,
        booking["id"],
        intent.json()["transaction_ref"],
        intent.json()["payment_intent_id"],
    )

    refund = client.post(
        "/payments/refund",
        headers=headers,
        json={"booking_id": booking["id"], "reason": "Customer requested refund"},
    )
    reconciliation = client.get("/payments/admin/reconciliation", headers=headers)
    transactions = client.get("/payments/transactions", headers=headers)
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()

    assert success.status_code == 200
    assert refund.status_code == 200
    assert refund.json()["message"] == "Refund recorded successfully"
    assert booking_row.payment_status == models.PaymentStatus.REFUNDED
    assert booking_row.status == models.BookingStatus.CANCELLED
    assert reconciliation.status_code == 200
    assert reconciliation.json()["refunded_attempts"] >= 1
    assert transactions.status_code == 200
    assert transactions.json()["total"] >= 1


def test_refund_rejects_unpaid_bookings(client, create_booking, db_session):
    headers = admin_headers(client, db_session)
    booking = create_booking()

    refund = client.post(
        "/payments/refund",
        headers=headers,
        json={"booking_id": booking["id"], "reason": "Should fail"},
    )

    assert refund.status_code == 400
    assert refund.json()["detail"] == "Only paid bookings can be refunded"


def test_payment_intent_creation_is_rate_limited(client, create_booking, db_session):
    booking = create_booking()

    for attempt in range(10):
        intent = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking["id"],
                "payment_method": "mock",
                "idempotency_key": f"rate-limit-{attempt}",
            },
        )
        assert intent.status_code == 200
        failure = client.post(
            "/payments/payment-failure",
            params={
                "booking_id": booking["id"],
                "transaction_ref": intent.json()["transaction_ref"],
                "reason": "Reset active attempt",
            },
        )
        assert failure.status_code == 200
        transactions = db_session.query(models.Transaction).filter_by(booking_id=booking["id"]).all()
        for transaction in transactions:
            transaction.created_at = transaction.created_at - timedelta(hours=2)
        db_session.commit()

    limited = client.post(
        "/payments/create-payment-intent",
        json={
            "booking_id": booking["id"],
            "payment_method": "mock",
            "idempotency_key": "rate-limit-final",
        },
    )

    assert limited.status_code == 429
    assert limited.json()["detail"] == "Too many requests. Please try again later."


def test_payment_intent_rejects_invalid_payment_method(client, create_booking):
    booking = create_booking()

    response = client.post(
        "/payments/create-payment-intent",
        json={
            "booking_id": booking["id"],
            "payment_method": "paypal",
            "idempotency_key": "invalid-pay-001",
        },
    )

    assert response.status_code == 422


def test_repeated_failed_payments_trigger_temporary_block(client, create_booking, db_session):
    booking = create_booking()

    for idx in range(3):
        transaction = models.Transaction(
            booking_id=booking["id"],
            transaction_ref=f"TXN-BLOCK-{idx}",
            amount=booking["total_amount"],
            currency="USD",
            payment_method="card",
            status=models.TransactionStatus.FAILED,
            failure_reason="Declined",
        )
        db_session.add(transaction)
    db_session.commit()

    response = client.post(
        "/payments/create-payment-intent",
        json={
            "booking_id": booking["id"],
            "payment_method": "card",
            "idempotency_key": "blocked-pay-001",
        },
    )

    assert response.status_code == 429
    assert (
        response.json()["detail"]
        == "Payment attempts temporarily blocked due to repeated failures"
    )


def test_reconciliation_expires_stale_processing_payment(client, create_booking, db_session):
    headers = admin_headers(client, db_session)
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_stale_001", "secret_stale_001"),
    ):
        intent = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "stale-001"},
        )

    acknowledge_card_payment(
        client,
        booking["id"],
        intent.json()["transaction_ref"],
        intent.json()["payment_intent_id"],
    )

    txn = db_session.query(models.Transaction).filter_by(booking_id=booking["id"]).first()
    txn.created_at = txn.created_at - timedelta(minutes=60)
    db_session.commit()

    reconcile = client.post(
        "/payments/reconcile-stuck",
        headers=headers,
        params={"timeout_minutes": 30},
    )

    db_session.refresh(txn)
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    inventory_rows = (
        db_session.query(models.RoomInventory)
        .filter(models.RoomInventory.room_id == booking["room_id"])
        .all()
    )
    assert reconcile.status_code == 200
    assert reconcile.json()["updated"] == 1
    assert txn.status == models.TransactionStatus.EXPIRED
    assert booking_row.payment_status == models.PaymentStatus.EXPIRED
    assert booking_row.status == models.BookingStatus.EXPIRED
    assert all(row.locked_units == 0 for row in inventory_rows)


def test_create_payment_intent_blocks_expired_hold(client, create_booking, db_session):
    booking = create_booking()
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    booking_row.hold_expires_at = booking_row.created_at - timedelta(minutes=1)
    db_session.commit()

    response = client.post(
        "/payments/create-payment-intent",
        json={"booking_id": booking["id"], "payment_method": "mock"},
    )

    db_session.refresh(booking_row)
    assert response.status_code == 400
    assert response.json()["detail"] == "Cancelled or expired bookings cannot be paid"
    assert booking_row.status == models.BookingStatus.EXPIRED
