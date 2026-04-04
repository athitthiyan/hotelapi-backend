"""
Production-grade payment retry scenario tests.

These tests verify the full retry lifecycle: card decline → inventory kept locked →
retry succeeds / hold expires → inventory released.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import models
from services.inventory_service import is_booking_inventory_locked


# ─── Helpers ─────────────────────────────────────────────────────────────────


def stripe_intent(intent_id: str, client_secret: str = "secret"):
    return MagicMock(id=intent_id, client_secret=client_secret)


def create_payment_intent(client, booking_id: int, idempotency_key: str | None = None):
    """Create a payment intent and return (response, payment_intent_id)."""
    # idempotency_key must be >= 8 chars per schema validation
    key = idempotency_key or "default-key-001"
    intent_id = f"pi_{key[:16]}"
    payload = {"booking_id": booking_id, "payment_method": "card", "idempotency_key": key}
    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent(intent_id),
    ):
        resp = client.post("/payments/create-payment-intent", json=payload)
    pi_id = resp.json().get("payment_intent_id") if resp.status_code == 200 else None
    return resp, pi_id


def record_failure(client, booking_id: int, reason: str = "Card declined",
                   payment_intent_id: str | None = None):
    """Record a payment failure, optionally referencing the pending transaction."""
    params = {"booking_id": booking_id, "reason": reason}
    if payment_intent_id:
        params["payment_intent_id"] = payment_intent_id
    return client.post("/payments/payment-failure", params=params)


def confirm_success(client, booking_id: int, payment_intent_id: str = "pi_success"):
    return client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking_id,
            "transaction_ref": f"TXN-{payment_intent_id.upper()[:12]}",
            "payment_intent_id": payment_intent_id,
            "payment_method": "card",
            "card_last4": "4242",
            "card_brand": "visa",
        },
    )


# ─── Core Retry Tests ─────────────────────────────────────────────────────────


def test_inventory_stays_locked_after_payment_failure_within_hold_window(
    client, create_booking, db_session
):
    """After a card decline, inventory stays locked while hold is still valid so retry is instant."""
    booking_data = create_booking()
    booking_id = booking_data["id"]

    # Create payment intent (locks inventory via ensure_booking_can_accept_payment)
    intent_resp, pi_id = create_payment_intent(client, booking_id, idempotency_key="attempt-key-001")
    assert intent_resp.status_code == 200

    # Record card failure, passing the payment_intent_id so it marks the PENDING txn FAILED
    fail_resp = record_failure(client, booking_id, reason="Your card was declined.",
                               payment_intent_id=pi_id)
    assert fail_resp.status_code == 200

    # Reload booking from DB
    db_session.expire_all()
    booking = db_session.query(models.Booking).filter_by(id=booking_id).first()

    # Key assertion: inventory must still be locked so the user can retry
    assert is_booking_inventory_locked(db_session, booking=booking), (
        "Inventory should remain locked after decline while hold is still valid"
    )
    assert booking.payment_status == models.PaymentStatus.FAILED


def test_inventory_released_after_payment_failure_when_hold_expired(
    client, create_booking, db_session
):
    """When the hold window has expired, a payment failure should release inventory."""
    booking_data = create_booking()
    booking_id = booking_data["id"]

    _, pi_id = create_payment_intent(client, booking_id, idempotency_key="expired-key-001")

    # Manually expire the hold before recording the failure
    booking = db_session.query(models.Booking).filter_by(id=booking_id).first()
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    fail_resp = record_failure(client, booking_id, reason="Card declined (expired hold)",
                               payment_intent_id=pi_id)
    assert fail_resp.status_code == 200

    db_session.expire_all()
    booking = db_session.query(models.Booking).filter_by(id=booking_id).first()

    # Inventory should be released because hold was expired
    assert not is_booking_inventory_locked(db_session, booking=booking), (
        "Inventory should be released when hold has expired at time of failure"
    )


def test_retry_payment_intent_succeeds_after_card_decline(client, create_booking, db_session):
    """User can create a new payment intent after a card decline — same booking_id."""
    booking_data = create_booking()
    booking_id = booking_data["id"]

    # First attempt — declines
    create_payment_intent(client, booking_id, idempotency_key="key-attempt1")
    record_failure(client, booking_id, reason="Your card was declined.")

    # Second attempt — new idempotency key (simulates user trying a different card)
    retry_intent = create_payment_intent(client, booking_id, idempotency_key="key-attempt2")
    assert retry_intent.status_code == 200, (
        f"Retry should succeed after decline within hold window. "
        f"Got {retry_intent.status_code}: {retry_intent.text}"
    )


def test_retry_reuses_same_booking_id_and_links_transaction_chain(
    client, create_booking, db_session
):
    """Each attempt creates a new transaction under the same booking — count increases."""
    booking_data = create_booking()
    booking_id = booking_data["id"]

    create_payment_intent(client, booking_id, idempotency_key="chain-1")
    record_failure(client, booking_id, reason="Decline #1")

    txn_count_after_1 = (
        db_session.query(models.Transaction).filter_by(booking_id=booking_id).count()
    )

    create_payment_intent(client, booking_id, idempotency_key="chain-2")
    record_failure(client, booking_id, reason="Decline #2")

    txn_count_after_2 = (
        db_session.query(models.Transaction).filter_by(booking_id=booking_id).count()
    )

    assert txn_count_after_2 > txn_count_after_1, (
        "Each retry attempt should create a new transaction row"
    )


def test_idempotent_payment_intent_returns_same_transaction(client, create_booking, db_session):
    """Sending the same idempotency_key twice returns the same transaction (no duplicates)."""
    booking_data = create_booking()
    booking_id = booking_data["id"]

    first = create_payment_intent(client, booking_id, idempotency_key="idem-retry-001")
    assert first.status_code == 200

    # Resend exact same key — should not create a duplicate transaction
    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_idem-retry-001"),
    ):
        second_resp = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking_id,
                "payment_method": "card",
                "idempotency_key": "idem-retry-001",
            },
        )

    assert second_resp.status_code == 200
    # Should not have doubled the transaction count
    txn_count = (
        db_session.query(models.Transaction).filter_by(booking_id=booking_id).count()
    )
    assert txn_count == 1


def test_payment_status_polling_returns_correct_state_after_failure(
    client, create_booking, db_session
):
    """GET /payments/status/{booking_id} reflects payment_status=failed after a decline."""
    booking_data = create_booking()
    booking_id = booking_data["id"]

    create_payment_intent(client, booking_id, idempotency_key="status-check")
    record_failure(client, booking_id, reason="Network failure")

    status_resp = client.get(f"/payments/status/{booking_id}")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["payment_status"] == "failed"
    assert body["latest_transaction"]["status"] == "failed"


def test_double_failure_then_success_full_retry_chain(client, create_booking, db_session):
    """Two consecutive declines followed by a successful payment — full retry chain works."""
    booking_data = create_booking()
    booking_id = booking_data["id"]

    # Decline #1
    create_payment_intent(client, booking_id, idempotency_key="full-chain-1")
    r = record_failure(client, booking_id, reason="Decline #1")
    assert r.status_code == 200

    # Decline #2
    r2_intent = create_payment_intent(client, booking_id, idempotency_key="full-chain-2")
    assert r2_intent.status_code == 200, f"2nd intent failed: {r2_intent.text}"
    r2_fail = record_failure(client, booking_id, reason="Decline #2")
    assert r2_fail.status_code == 200

    # Successful payment on 3rd attempt
    r3_intent = create_payment_intent(client, booking_id, idempotency_key="full-chain-3")
    assert r3_intent.status_code == 200, f"3rd intent failed: {r3_intent.text}"

    success_resp = confirm_success(client, booking_id, payment_intent_id="pi_full_chain_3")
    assert success_resp.status_code == 200

    db_session.expire_all()
    booking = db_session.query(models.Booking).filter_by(id=booking_id).first()
    assert booking.payment_status == models.PaymentStatus.PAID
    assert booking.status == models.BookingStatus.CONFIRMED


def test_conflict_409_on_retry_when_dates_taken(client, create_booking, db_session, room_id):
    """
    When inventory is released (expired hold) before the retry, create-payment-intent
    returns 409 so the frontend can show the conflict state.
    """
    # Booking A acquires the slot
    booking_data = create_booking()
    booking_id = booking_data["id"]

    create_payment_intent(client, booking_id, idempotency_key="conflict-1")
    record_failure(client, booking_id, reason="Declined")

    # Force-expire the hold AND release inventory manually to simulate external cleanup
    booking = db_session.query(models.Booking).filter_by(id=booking_id).first()
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    # Also remove inventory lock rows so the slot appears free for booking B
    db_session.query(models.RoomInventory).filter_by(
        locked_by_booking_id=booking_id
    ).update(
        {
            "locked_units": 0,
            "locked_by_booking_id": None,
            "lock_expires_at": None,
            "available_units": 0,           # another booking took it
            "status": models.InventoryStatus.BLOCKED,
        }
    )
    db_session.commit()

    # Retry should now get 409 because hold expired and inventory is blocked
    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_conflict"),
    ):
        retry_resp = client.post(
            "/payments/create-payment-intent",
            json={
                "booking_id": booking_id,
                "payment_method": "card",
                "idempotency_key": "conflict-2",
            },
        )

    # Should be 4xx (400 for expired hold or 409 for conflict)
    assert retry_resp.status_code in (400, 409), (
        f"Expected 400/409 when hold expired and inventory blocked. "
        f"Got {retry_resp.status_code}: {retry_resp.text}"
    )
