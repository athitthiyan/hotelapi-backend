from unittest.mock import patch

import models


def payment_success(client, booking_id: int, ref: str, payment_intent_id: str | None = None):
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


def test_create_payment_intent_blocks_paid_booking(client, create_booking):
    booking = create_booking()
    success = payment_success(client, booking["id"], "TXN-PAID001", "pi_paid_001")
    assert success.status_code == 200

    with patch("routers.payments.stripe.PaymentIntent.create"):
        response = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Booking already paid"


def test_payment_success_is_idempotent(client, create_booking, db_session):
    booking = create_booking()

    first = payment_success(client, booking["id"], "TXN-IDEMP001", "pi_idemp_001")
    second = payment_success(client, booking["id"], "TXN-IDEMP002", "pi_idemp_001")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    txn_count = db_session.query(models.Transaction).count()
    paid_booking = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert txn_count == 1
    assert paid_booking.payment_status == models.PaymentStatus.PAID


def test_failed_payment_can_retry_and_then_succeed(client, create_booking, db_session):
    booking = create_booking()

    fail = client.post(
        "/payments/payment-failure",
        params={"booking_id": booking["id"], "reason": "Card declined"},
    )
    success = payment_success(client, booking["id"], "TXN-RETRY001", "pi_retry_001")

    assert fail.status_code == 200
    assert success.status_code == 200

    txns = (
        db_session.query(models.Transaction)
        .filter(models.Transaction.booking_id == booking["id"])
        .order_by(models.Transaction.id.asc())
        .all()
    )
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert len(txns) == 2
    assert txns[0].status == models.TransactionStatus.FAILED
    assert txns[1].status == models.TransactionStatus.SUCCESS
    assert booking_row.payment_status == models.PaymentStatus.PAID
    assert booking_row.status == models.BookingStatus.CONFIRMED


def test_payment_failure_rejected_when_booking_is_already_paid(client, create_booking):
    booking = create_booking()
    success = payment_success(client, booking["id"], "TXN-LOCK001", "pi_lock_001")
    assert success.status_code == 200

    fail = client.post(
        "/payments/payment-failure",
        params={"booking_id": booking["id"], "reason": "Should not record"},
    )
    assert fail.status_code == 409


def test_payment_status_returns_latest_transaction(client, create_booking):
    booking = create_booking()
    client.post(
        "/payments/payment-failure",
        params={"booking_id": booking["id"], "reason": "Retry later"},
    )

    response = client.get(f"/payments/status/{booking['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["booking_id"] == booking["id"]
    assert body["payment_status"] == "failed"
    assert body["latest_transaction"]["status"] == "failed"


def test_webhook_success_is_idempotent(client, create_booking, db_session):
    booking = create_booking()
    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_webhook_success_001",
                "metadata": {"booking_id": str(booking["id"])},
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

    assert first.status_code == 200
    assert second.status_code == 200

    txns = db_session.query(models.Transaction).filter_by(booking_id=booking["id"]).all()
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert len(txns) == 1
    assert txns[0].status == models.TransactionStatus.SUCCESS
    assert booking_row.payment_status == models.PaymentStatus.PAID


def test_webhook_failure_marks_booking_failed(client, create_booking, db_session):
    booking = create_booking()
    event = {
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_webhook_failed_001",
                "metadata": {"booking_id": str(booking["id"])},
                "last_payment_error": {"message": "Insufficient funds"},
            }
        },
    }

    with patch("routers.payments.stripe.Webhook.construct_event", return_value=event):
        response = client.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig"})

    assert response.status_code == 200

    txns = db_session.query(models.Transaction).filter_by(booking_id=booking["id"]).all()
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    assert len(txns) == 1
    assert txns[0].status == models.TransactionStatus.FAILED
    assert txns[0].failure_reason == "Insufficient funds"
    assert booking_row.payment_status == models.PaymentStatus.FAILED
