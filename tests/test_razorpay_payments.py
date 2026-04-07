"""
Comprehensive tests for Razorpay payment flow (Phases 1-7).

Coverage targets:
  1. Success path (create order → verify payment → confirm)
  2. Payment failure recording
  3. Retry after failure
  4. Popup close handling
  5. Duplicate webhook (idempotency)
  6. UPI pending state
  7. Hold expiry during payment (auto-refund)
  8. Refund (full + partial)
  9. Webhook signature verification
  10. Multi-tab sync (status polling)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import models
from database import settings

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_razorpay_signature(order_id: str, payment_id: str, secret: str) -> str:
    """Build an HMAC SHA-256 signature identical to what Razorpay sends."""
    payload = f"{order_id}|{payment_id}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_webhook_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_payload(event: str, entity_data: dict, wrapper_key: str = "payment") -> dict:
    """Build a Razorpay webhook event payload."""
    return {
        "event": event,
        "payload": {
            wrapper_key: {
                "entity": entity_data,
            }
        },
    }


def _create_room(db) -> models.Room:
    room = models.Room(
        hotel_name="Test Hotel",
        room_type=models.RoomType.DELUXE,
        description="Test",
        price=2500.0,
        availability=True,
        city="Mumbai",
        country="India",
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    return room


def _create_booking(
    db,
    room: models.Room,
    *,
    status: models.BookingStatus = models.BookingStatus.PENDING,
    payment_status: models.PaymentStatus = models.PaymentStatus.PENDING,
    hold_expires_at: datetime | None = None,
    total_amount: float = 2500.0,
) -> models.Booking:
    booking = models.Booking(
        booking_ref=f"BK-{secrets.token_hex(4).upper()}",
        user_name="Test User",
        email="test@example.com",
        phone="9876543210",
        room_id=room.id,
        check_in=_utc_now() + timedelta(days=1),
        check_out=_utc_now() + timedelta(days=3),
        guests=2,
        nights=2,
        room_rate=room.price,
        taxes=250.0,
        service_fee=50.0,
        total_amount=total_amount,
        status=status,
        payment_status=payment_status,
        hold_expires_at=hold_expires_at or (_utc_now() + timedelta(minutes=10)),
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return booking


def _create_transaction(
    db,
    booking: models.Booking,
    *,
    razorpay_order_id: str = "order_test_123",
    razorpay_payment_id: str | None = None,
    status: models.TransactionStatus = models.TransactionStatus.PENDING,
    payment_method: str = "upi",
    idempotency_key: str | None = None,
    gateway: str = "razorpay",
) -> models.Transaction:
    txn_ref = f"TXN-RZP-{secrets.token_hex(6).upper()}"
    txn = models.Transaction(
        booking_id=booking.id,
        transaction_ref=txn_ref,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        gateway=gateway,
        idempotency_key=idempotency_key or txn_ref,
        amount=booking.total_amount,
        currency="INR",
        payment_method=payment_method,
        status=status,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


# Fake Razorpay client for mocking
class FakeRazorpayOrder:
    def create(self, data: dict) -> dict:
        return {"id": f"order_rzp_{secrets.token_hex(6)}"}


class FakeRazorpayPayment:
    """Simulates Razorpay payment API."""

    def __init__(self, status: str = "captured", amount: int = 250000):
        self._status = status
        self._amount = amount
        self._order_id = ""

    def fetch(self, payment_id: str) -> dict:
        return {
            "id": payment_id,
            "status": self._status,
            "order_id": self._order_id,
            "amount": self._amount,
            "method": "upi",
            "card": {"last4": "1234", "network": "Visa"},
        }

    def capture(self, payment_id: str, amount: int) -> dict:
        return {"id": payment_id, "status": "captured"}

    def refund(self, payment_id: str, data: dict) -> dict:
        return {"id": f"rfnd_{secrets.token_hex(6)}", "status": "processed"}


class FakeRazorpayClient:
    def __init__(
        self,
        payment_status: str = "captured",
        amount: int = 250000,
        fail_refund: bool = False,
        fail_order: bool = False,
    ):
        self.order = FakeRazorpayOrder()
        self.payment = FakeRazorpayPayment(status=payment_status, amount=amount)
        self._fail_refund = fail_refund
        self._fail_order = fail_order
        if fail_order:
            self.order.create = self._fail_order_create
        if fail_refund:
            self.payment.refund = self._fail_refund_call

    def _fail_order_create(self, data: dict) -> dict:
        raise Exception("Razorpay API unavailable")

    def _fail_refund_call(self, payment_id: str, data: dict) -> dict:
        raise Exception("Refund API error")


def _mock_razorpay_client(
    payment_status: str = "captured",
    amount: int = 250000,
    order_id: str = "",
    fail_refund: bool = False,
    fail_order: bool = False,
):
    """Return a patch context manager that replaces _get_razorpay_client."""
    client = FakeRazorpayClient(
        payment_status=payment_status,
        amount=amount,
        fail_refund=fail_refund,
        fail_order=fail_order,
    )
    if order_id:
        client.payment._order_id = order_id
    return patch(
        "routers.razorpay_payments._get_razorpay_client",
        return_value=client,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Admin helper fixture
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def admin_user(db_session):
    """Create an admin user and return auth headers."""
    from routers.auth import pwd_context

    user = models.User(
        email="admin@stayvora.co.in",
        full_name="Admin",
        hashed_password=pwd_context.hash("AdminPass123"),
        is_admin=True,
        is_email_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def admin_headers(client, admin_user):
    resp = client.post(
        "/auth/login",
        json={"email": "admin@stayvora.co.in", "password": "AdminPass123"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────────────────────────
# 1. Create Order
# ──────────────────────────────────────────────────────────────────────────────


class TestCreateOrder:
    """Phase 1-2: Order creation with idempotency."""

    def test_create_order_upi(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "upi",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "order_id" in data
        assert data["currency"] == "INR"
        assert data["amount_paise"] == int(booking.total_amount * 100)
        assert data["idempotent"] is False

    def test_create_order_card(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "card",
                },
            )
        assert resp.status_code == 200

    def test_create_order_netbanking(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "netbanking",
                },
            )
        assert resp.status_code == 200

    def test_create_order_wallet(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "wallet",
                },
            )
        assert resp.status_code == 200

    def test_create_order_mock_method(self, client, db_session):
        """Mock method should create order without calling Razorpay API."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "mock",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"].startswith("order_mock_")

    def test_create_order_phonepay_normalised(self, client, db_session):
        """'phonepay' should be normalised to 'phonepe'."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "phonepay",
                },
            )
        assert resp.status_code == 200

    def test_create_order_invalid_method(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "bitcoin",
                },
            )
        assert resp.status_code == 422

    def test_create_order_booking_not_found(self, client, db_session):
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={"booking_id": 99999, "payment_method": "upi"},
            )
        assert resp.status_code == 404

    def test_create_order_already_paid(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={"booking_id": booking.id, "payment_method": "upi"},
            )
        assert resp.status_code == 409

    def test_create_order_cancelled_booking(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.CANCELLED,
        )
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={"booking_id": booking.id, "payment_method": "upi"},
            )
        assert resp.status_code == 400

    def test_create_order_expired_booking(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.EXPIRED,
        )
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={"booking_id": booking.id, "payment_method": "upi"},
            )
        assert resp.status_code == 400

    def test_create_order_idempotency_key(self, client, db_session):
        """Same idempotency_key should return cached order."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        idem_key = "idem-test-001"

        with _mock_razorpay_client():
            resp1 = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "upi",
                    "idempotency_key": idem_key,
                },
            )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["idempotent"] is False

        # Second call with same key returns cached
        with _mock_razorpay_client():
            resp2 = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "upi",
                    "idempotency_key": idem_key,
                },
            )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["idempotent"] is True
        assert data2["order_id"] == data1["order_id"]

    def test_create_order_razorpay_api_failure(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client(fail_order=True):
            resp = client.post(
                "/payments/razorpay/create-order",
                json={"booking_id": booking.id, "payment_method": "upi"},
            )
        assert resp.status_code == 502

    def test_create_order_sets_processing_status(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={"booking_id": booking.id, "payment_method": "upi"},
            )
        assert resp.status_code == 200

        db_session.refresh(booking)
        assert booking.status == models.BookingStatus.PROCESSING
        assert booking.payment_status == models.PaymentStatus.PROCESSING

    def test_create_order_razorpay_not_configured(self, client, db_session):
        """If razorpay keys are empty, should return 503."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        with patch("routers.razorpay_payments._get_razorpay_client") as mock_client:
            from fastapi import HTTPException
            mock_client.side_effect = HTTPException(status_code=503, detail="Razorpay is not configured")
            resp = client.post(
                "/payments/razorpay/create-order",
                json={"booking_id": booking.id, "payment_method": "upi"},
            )
        assert resp.status_code == 503


# ──────────────────────────────────────────────────────────────────────────────
# 2. Verify Payment (Frontend Callback) — Happy Path
# ──────────────────────────────────────────────────────────────────────────────


class TestVerifyPayment:
    """Phase 3: Payment verification with HMAC signature."""

    def _setup(self, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        order_id = "order_verify_test_001"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)
        return room, booking, txn, order_id

    def test_verify_success(self, client, db_session):
        room, booking, txn, order_id = self._setup(db_session)
        payment_id = "pay_test_success_001"
        key_secret = settings.razorpay_key_secret or "test_secret"

        with patch.object(settings, "razorpay_key_secret", key_secret):
            sig = _make_razorpay_signature(order_id, payment_id, key_secret)
            with _mock_razorpay_client(amount=int(booking.total_amount * 100), order_id=order_id):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["booking_status"] == "confirmed"

        db_session.refresh(booking)
        assert booking.status == models.BookingStatus.CONFIRMED
        assert booking.payment_status == models.PaymentStatus.PAID

        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.SUCCESS
        assert txn.razorpay_payment_id == payment_id

    def test_verify_invalid_signature(self, client, db_session):
        room, booking, txn, order_id = self._setup(db_session)
        key_secret = "test_secret"

        with patch.object(settings, "razorpay_key_secret", key_secret):
            with _mock_razorpay_client():
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": "pay_bad",
                        "razorpay_signature": "invalid_signature_here",
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()

        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.FAILED

    def test_verify_transaction_not_found(self, client, db_session):
        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": "order_ghost",
                "razorpay_payment_id": "pay_ghost",
                "razorpay_signature": "sig",
                "transaction_ref": "TXN-DOES-NOT-EXIST",
            },
        )
        assert resp.status_code == 404

    def test_verify_already_success_idempotent(self, client, db_session):
        """If transaction is already SUCCESS, return immediately (idempotent)."""
        room, booking, txn, order_id = self._setup(db_session)
        txn.status = models.TransactionStatus.SUCCESS
        txn.razorpay_payment_id = "pay_existing"
        db_session.commit()

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": "pay_existing",
                "razorpay_signature": "anything",
                "transaction_ref": txn.transaction_ref,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Payment already confirmed"

    def test_verify_key_secret_not_configured(self, client, db_session):
        room, booking, txn, order_id = self._setup(db_session)

        with patch.object(settings, "razorpay_key_secret", ""):
            resp = client.post(
                "/payments/razorpay/verify-payment",
                json={
                    "razorpay_order_id": order_id,
                    "razorpay_payment_id": "pay_test",
                    "razorpay_signature": "sig",
                    "transaction_ref": txn.transaction_ref,
                },
            )
        assert resp.status_code == 503

    def test_verify_payment_not_captured(self, client, db_session):
        """If Razorpay says payment is 'failed' status, should reject."""
        room, booking, txn, order_id = self._setup(db_session)
        payment_id = "pay_failed"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        with patch.object(settings, "razorpay_key_secret", key_secret):
            with _mock_razorpay_client(payment_status="failed", order_id=order_id):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 400

    def test_verify_order_id_mismatch(self, client, db_session):
        """If Razorpay returns a different order_id, should fail."""
        room, booking, txn, order_id = self._setup(db_session)
        payment_id = "pay_mismatch"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        with patch.object(settings, "razorpay_key_secret", key_secret):
            # order_id in Razorpay response doesn't match
            with _mock_razorpay_client(
                amount=int(booking.total_amount * 100),
                order_id="order_DIFFERENT",
            ):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 400
        assert "mismatch" in resp.json()["detail"].lower()

    def test_verify_amount_mismatch(self, client, db_session):
        """If Razorpay payment amount doesn't match transaction, should fail."""
        room, booking, txn, order_id = self._setup(db_session)
        payment_id = "pay_amt_mismatch"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        with patch.object(settings, "razorpay_key_secret", key_secret):
            with _mock_razorpay_client(amount=99999, order_id=order_id):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 400
        assert "amount" in resp.json()["detail"].lower()

    def test_verify_payment_authorized_capture(self, client, db_session):
        """If payment status is 'authorized', verify should capture it."""
        room, booking, txn, order_id = self._setup(db_session)
        payment_id = "pay_auth"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        with patch.object(settings, "razorpay_key_secret", key_secret):
            with _mock_razorpay_client(
                payment_status="authorized",
                amount=int(booking.total_amount * 100),
                order_id=order_id,
            ):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_verify_capture_fails(self, client, db_session):
        """If capture call to Razorpay fails, transaction should be marked FAILED."""
        room, booking, txn, order_id = self._setup(db_session)
        payment_id = "pay_capture_fail"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        with patch.object(settings, "razorpay_key_secret", key_secret):
            mock_client = FakeRazorpayClient(payment_status="authorized")
            mock_client.payment._order_id = order_id
            mock_client.payment.capture = MagicMock(side_effect=Exception("Capture timeout"))
            with patch("routers.razorpay_payments._get_razorpay_client", return_value=mock_client):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 400
        assert "capture failed" in resp.json()["detail"].lower()

    def test_verify_fetch_payment_api_error(self, client, db_session):
        """If fetching payment from Razorpay fails, should return 502."""
        room, booking, txn, order_id = self._setup(db_session)
        payment_id = "pay_fetch_err"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        with patch.object(settings, "razorpay_key_secret", key_secret):
            mock_client = FakeRazorpayClient()
            mock_client.payment.fetch = MagicMock(side_effect=Exception("API down"))
            with patch("routers.razorpay_payments._get_razorpay_client", return_value=mock_client):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 502

    def test_verify_booking_not_found_returns_warning(self, client, db_session):
        """If booking was deleted but transaction exists, return success with warning."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_orphan"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)
        booking_id = booking.id
        payment_id = "pay_orphan"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        # Delete the booking row directly (bypass ORM cascade) so transaction becomes orphaned
        db_session.query(models.NotificationOutbox).filter_by(booking_id=booking_id).delete()
        db_session.execute(
            models.Booking.__table__.delete().where(models.Booking.__table__.c.id == booking_id)
        )
        db_session.commit()

        with patch.object(settings, "razorpay_key_secret", key_secret):
            with _mock_razorpay_client(
                amount=int(2500 * 100),
                order_id=order_id,
            ):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )
        assert resp.status_code == 200
        assert "warning" in resp.json()


# ──────────────────────────────────────────────────────────────────────────────
# 3. Payment Failure
# ──────────────────────────────────────────────────────────────────────────────


class TestPaymentFailure:
    """Phase 4: Failure recording (popup close, decline, timeout)."""

    def test_record_failure(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_fail_001"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        resp = client.post(
            "/payments/razorpay/payment-failure",
            json={
                "razorpay_order_id": order_id,
                "error_code": "PAYMENT_CANCELLED",
                "error_description": "User closed popup",
                "error_reason": "user_cancel",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "recorded"

        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.FAILED
        assert "PAYMENT_CANCELLED" in txn.failure_reason

        db_session.refresh(booking)
        assert booking.payment_status == models.PaymentStatus.FAILED

    def test_record_failure_no_error_details(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_fail_002"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        resp = client.post(
            "/payments/razorpay/payment-failure",
            json={"razorpay_order_id": order_id},
        )
        assert resp.status_code == 200
        db_session.refresh(txn)
        assert "cancelled by user" in txn.failure_reason.lower()

    def test_record_failure_already_paid(self, client, db_session):
        """If transaction already succeeded, failure should not overwrite."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_fail_003"
        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id=order_id,
            status=models.TransactionStatus.SUCCESS,
        )

        resp = client.post(
            "/payments/razorpay/payment-failure",
            json={"razorpay_order_id": order_id},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_paid"

        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.SUCCESS

    def test_record_failure_transaction_not_found(self, client, db_session):
        resp = client.post(
            "/payments/razorpay/payment-failure",
            json={"razorpay_order_id": "order_nonexistent"},
        )
        assert resp.status_code == 404

    def test_record_failure_booking_already_paid_no_overwrite(self, client, db_session):
        """Booking payment_status should not change if already PAID."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        # Create a second transaction that failed (retry scenario)
        order_id = "order_fail_retry"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        resp = client.post(
            "/payments/razorpay/payment-failure",
            json={
                "razorpay_order_id": order_id,
                "error_code": "TIMEOUT",
            },
        )
        # Transaction is already SUCCESS, so this should not crash
        # But booking is already paid, so payment_status should remain PAID
        db_session.refresh(booking)
        assert booking.payment_status == models.PaymentStatus.PAID


# ──────────────────────────────────────────────────────────────────────────────
# 4. Retry After Failure
# ──────────────────────────────────────────────────────────────────────────────


class TestRetryAfterFailure:
    """Phase 4: Retry after failure — user can create a new order."""

    def test_retry_creates_new_transaction(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.FAILED,
        )
        # First failed transaction exists
        _create_transaction(
            db_session, booking,
            razorpay_order_id="order_old",
            status=models.TransactionStatus.FAILED,
        )

        # Create new order (retry)
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/create-order",
                json={
                    "booking_id": booking.id,
                    "payment_method": "card",
                },
            )
        assert resp.status_code == 200

        # Should have 2 transactions now
        txn_count = (
            db_session.query(models.Transaction)
            .filter(models.Transaction.booking_id == booking.id)
            .count()
        )
        assert txn_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# 5. Webhook Handler
# ──────────────────────────────────────────────────────────────────────────────


class TestWebhook:
    """Phase 5: Webhook — payment.captured, payment.failed, refund.processed."""

    def test_webhook_payment_captured(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        order_id = "order_wh_001"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        payload = _webhook_payload("payment.captured", {
            "id": "pay_wh_001",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "upi",
            "card": {},
        })

        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmed"

        db_session.refresh(booking)
        assert booking.status == models.BookingStatus.CONFIRMED
        assert booking.payment_status == models.PaymentStatus.PAID

        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.SUCCESS

    def test_webhook_payment_captured_idempotent(self, client, db_session):
        """Duplicate webhook should return already_processed."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_wh_dup"
        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id=order_id,
            status=models.TransactionStatus.SUCCESS,
        )

        payload = _webhook_payload("payment.captured", {
            "id": "pay_wh_dup",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "upi",
        })

        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_processed"

    def test_webhook_payment_captured_no_order_id(self, client, db_session):
        payload = _webhook_payload("payment.captured", {"id": "pay_no_order"})
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_webhook_payment_captured_transaction_not_found(self, client, db_session):
        payload = _webhook_payload("payment.captured", {
            "id": "pay_missing",
            "order_id": "order_nonexistent",
            "amount": 100000,
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_webhook_payment_captured_booking_not_found(self, client, db_session):
        """If booking deleted but transaction exists, should still process gracefully."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_wh_orphan"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)
        booking_id = booking.id

        # Remove booking row directly (bypass ORM cascade)
        db_session.query(models.NotificationOutbox).filter_by(booking_id=booking_id).delete()
        db_session.execute(
            models.Booking.__table__.delete().where(models.Booking.__table__.c.id == booking_id)
        )
        db_session.commit()

        payload = _webhook_payload("payment.captured", {
            "id": "pay_orphan_wh",
            "order_id": order_id,
            "amount": 250000,
            "method": "upi",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "warning" in resp.json()

    def test_webhook_payment_failed(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_wh_fail"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        payload = _webhook_payload("payment.failed", {
            "id": "pay_wh_fail",
            "order_id": order_id,
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Card declined",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "recorded"

        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.FAILED

        db_session.refresh(booking)
        assert booking.payment_status == models.PaymentStatus.FAILED

    def test_webhook_payment_failed_no_order_id(self, client, db_session):
        payload = _webhook_payload("payment.failed", {"id": "pay_fail_no_order"})
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_webhook_payment_failed_already_succeeded(self, client, db_session):
        """Failed webhook should not overwrite a successful transaction."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_wh_fail_idm"
        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id=order_id,
            status=models.TransactionStatus.SUCCESS,
        )

        payload = _webhook_payload("payment.failed", {
            "id": "pay_late_fail",
            "order_id": order_id,
            "error_code": "TIMEOUT",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.SUCCESS

    def test_webhook_payment_failed_no_error_code(self, client, db_session):
        """Payment failed with empty error_code."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        order_id = "order_wh_fail_nocode"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        payload = _webhook_payload("payment.failed", {
            "id": "pay_nocode",
            "order_id": order_id,
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        db_session.refresh(txn)
        assert "webhook" in txn.failure_reason.lower()

    def test_webhook_refund_processed(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        booking.refund_status = models.RefundStatus.REFUND_INITIATED
        db_session.commit()

        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id="order_rfnd",
            razorpay_payment_id="pay_rfnd_001",
            status=models.TransactionStatus.SUCCESS,
        )

        payload = _webhook_payload(
            "refund.processed",
            {
                "id": "rfnd_webhook_001",
                "payment_id": "pay_rfnd_001",
                "status": "processed",
                "amount": int(booking.total_amount * 100),
            },
            wrapper_key="refund",
        )
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["refund_status"] == "processed"

        db_session.refresh(booking)
        assert booking.refund_status == models.RefundStatus.REFUND_SUCCESS
        assert booking.payment_status == models.PaymentStatus.REFUNDED
        assert booking.status == models.BookingStatus.CANCELLED

    def test_webhook_refund_failed(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        booking.refund_status = models.RefundStatus.REFUND_INITIATED
        db_session.commit()

        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id="order_rfnd_fail",
            razorpay_payment_id="pay_rfnd_fail",
            status=models.TransactionStatus.SUCCESS,
        )

        payload = _webhook_payload(
            "refund.processed",
            {
                "id": "rfnd_fail_001",
                "payment_id": "pay_rfnd_fail",
                "status": "failed",
                "amount": int(booking.total_amount * 100),
            },
            wrapper_key="refund",
        )
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        db_session.refresh(booking)
        assert booking.refund_status == models.RefundStatus.REFUND_FAILED

    def test_webhook_refund_already_processed_idempotent(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        booking.refund_status = models.RefundStatus.REFUND_SUCCESS
        db_session.commit()

        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id="order_rfnd_dup",
            razorpay_payment_id="pay_rfnd_dup",
            status=models.TransactionStatus.SUCCESS,
        )

        payload = _webhook_payload(
            "refund.processed",
            {
                "id": "rfnd_dup",
                "payment_id": "pay_rfnd_dup",
                "status": "processed",
                "amount": int(booking.total_amount * 100),
            },
            wrapper_key="refund",
        )
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_processed"

    def test_webhook_refund_no_payment_id(self, client, db_session):
        payload = _webhook_payload(
            "refund.processed",
            {"id": "rfnd_no_pid", "status": "processed"},
            wrapper_key="refund",
        )
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_webhook_refund_transaction_not_found(self, client, db_session):
        payload = _webhook_payload(
            "refund.processed",
            {
                "id": "rfnd_orphan",
                "payment_id": "pay_nonexistent",
                "status": "processed",
                "amount": 100000,
            },
            wrapper_key="refund",
        )
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_webhook_refund_booking_not_found(self, client, db_session):
        """Refund webhook with deleted booking."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        booking_id = booking.id
        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id="order_rfnd_orphan",
            razorpay_payment_id="pay_rfnd_orphan",
            status=models.TransactionStatus.SUCCESS,
        )
        db_session.query(models.NotificationOutbox).filter_by(booking_id=booking_id).delete()
        db_session.execute(
            models.Booking.__table__.delete().where(models.Booking.__table__.c.id == booking_id)
        )
        db_session.commit()

        payload = _webhook_payload(
            "refund.processed",
            {
                "id": "rfnd_orphan2",
                "payment_id": "pay_rfnd_orphan",
                "status": "processed",
                "amount": 100000,
            },
            wrapper_key="refund",
        )
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_webhook_unhandled_event(self, client, db_session):
        payload = {"event": "order.paid", "payload": {}}
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_webhook_invalid_json(self, client, db_session):
        resp = client.post(
            "/payments/razorpay/webhook",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_webhook_payment_failed_transaction_not_found(self, client, db_session):
        payload = _webhook_payload("payment.failed", {
            "id": "pay_fail_ghost",
            "order_id": "order_ghost",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_webhook_payment_captured_with_card_info(self, client, db_session):
        """Card details should be extracted from webhook payload."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        order_id = "order_wh_card"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        payload = _webhook_payload("payment.captured", {
            "id": "pay_wh_card",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "card",
            "card": {"last4": "4242", "network": "Visa"},
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        db_session.refresh(txn)
        assert txn.card_last4 == "4242"
        assert txn.card_brand == "Visa"

    def test_webhook_payment_captured_already_paid_booking(self, client, db_session):
        """If booking already PAID (via verify-payment), webhook should not re-process."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        order_id = "order_wh_already_paid"
        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id=order_id,
            status=models.TransactionStatus.PENDING,
        )

        payload = _webhook_payload("payment.captured", {
            "id": "pay_wh_already",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "upi",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        # Transaction should be updated to SUCCESS even though booking was already confirmed
        db_session.refresh(txn)
        assert txn.status == models.TransactionStatus.SUCCESS


# ──────────────────────────────────────────────────────────────────────────────
# 6. Webhook Signature Verification
# ──────────────────────────────────────────────────────────────────────────────


class TestWebhookSignature:
    """Phase 5: Webhook HMAC signature verification."""

    def test_valid_webhook_signature(self, client, db_session):
        webhook_secret = "whsec_test_123"
        payload = json.dumps({"event": "order.paid", "payload": {}}).encode()
        sig = _make_webhook_signature(payload, webhook_secret)

        with patch.object(settings, "razorpay_webhook_secret", webhook_secret):
            resp = client.post(
                "/payments/razorpay/webhook",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sig,
                },
            )
        assert resp.status_code == 200

    def test_invalid_webhook_signature(self, client, db_session):
        webhook_secret = "whsec_test_123"
        payload = json.dumps({"event": "payment.captured", "payload": {}}).encode()

        with patch.object(settings, "razorpay_webhook_secret", webhook_secret):
            resp = client.post(
                "/payments/razorpay/webhook",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": "bad_signature",
                },
            )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()

    def test_no_webhook_secret_configured_skips_verification(self, client, db_session):
        """If no webhook secret configured, skip signature check."""
        payload = json.dumps({"event": "order.paid", "payload": {}}).encode()

        with patch.object(settings, "razorpay_webhook_secret", ""):
            resp = client.post(
                "/payments/razorpay/webhook",
                content=payload,
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────────
# 7. Hold Expiry Edge Case (Auto-Refund)
# ──────────────────────────────────────────────────────────────────────────────


class TestHoldExpiry:
    """Phase 6: Hold expiry during payment — auto-refund."""

    def test_verify_payment_hold_expired_autorefund(self, client, db_session):
        """Payment arrives after hold expired → don't confirm, auto-refund."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
            hold_expires_at=_utc_now() - timedelta(minutes=5),  # expired 5 min ago
        )
        order_id = "order_expired_001"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)
        payment_id = "pay_expired_001"
        key_secret = "test_secret"
        sig = _make_razorpay_signature(order_id, payment_id, key_secret)

        with patch.object(settings, "razorpay_key_secret", key_secret):
            with _mock_razorpay_client(
                amount=int(booking.total_amount * 100),
                order_id=order_id,
            ):
                resp = client.post(
                    "/payments/razorpay/verify-payment",
                    json={
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": sig,
                        "transaction_ref": txn.transaction_ref,
                    },
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "expired"
        assert data["refund_requested"] is True

        db_session.refresh(booking)
        assert booking.status == models.BookingStatus.EXPIRED
        assert booking.refund_status == models.RefundStatus.REFUND_REQUESTED
        assert booking.refund_amount == booking.total_amount

    def test_webhook_payment_captured_hold_expired(self, client, db_session):
        """Webhook payment.captured after hold expiry → auto-refund."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
            hold_expires_at=_utc_now() - timedelta(minutes=3),
        )
        order_id = "order_wh_expired"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        payload = _webhook_payload("payment.captured", {
            "id": "pay_wh_expired",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "upi",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "expired_hold_autorefund"

        db_session.refresh(booking)
        assert booking.status == models.BookingStatus.EXPIRED
        assert booking.refund_status == models.RefundStatus.REFUND_REQUESTED

    def test_hold_not_expired_proceeds_normally(self, client, db_session):
        """If hold is still active, payment should succeed normally."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
            hold_expires_at=_utc_now() + timedelta(minutes=5),  # still active
        )
        order_id = "order_active_hold"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        payload = _webhook_payload("payment.captured", {
            "id": "pay_active_hold",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "upi",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    def test_hold_no_expiry_set(self, client, db_session):
        """Booking without hold_expires_at should not be treated as expired."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        booking.hold_expires_at = None
        db_session.commit()

        order_id = "order_no_hold"
        txn = _create_transaction(db_session, booking, razorpay_order_id=order_id)

        payload = _webhook_payload("payment.captured", {
            "id": "pay_no_hold",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "upi",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    def test_hold_expired_but_already_confirmed_skips_autorefund(self, client, db_session):
        """If booking already CONFIRMED, don't auto-refund even if hold expired."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
            hold_expires_at=_utc_now() - timedelta(minutes=5),
        )
        order_id = "order_wh_confirmed_expired"
        txn = _create_transaction(
            db_session, booking,
            razorpay_order_id=order_id,
            status=models.TransactionStatus.PENDING,
        )

        payload = _webhook_payload("payment.captured", {
            "id": "pay_confirmed_expired",
            "order_id": order_id,
            "amount": int(booking.total_amount * 100),
            "method": "upi",
        })
        resp = client.post(
            "/payments/razorpay/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        # Should NOT be expired_hold_autorefund since already confirmed
        assert resp.json()["status"] != "expired_hold_autorefund"


# ──────────────────────────────────────────────────────────────────────────────
# 8. Refund Flow (Full + Partial)
# ──────────────────────────────────────────────────────────────────────────────


class TestRefund:
    """Phase 7: Full and partial refund via Razorpay API."""

    def test_full_refund(self, client, db_session, admin_headers):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        txn = _create_transaction(
            db_session, booking,
            razorpay_payment_id="pay_refund_full",
            status=models.TransactionStatus.SUCCESS,
        )

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={
                    "booking_id": booking.id,
                    "reason": "Customer requested cancellation",
                },
                headers=admin_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "refund_initiated"
        assert data["refund_amount"] == booking.total_amount
        assert data["is_partial"] is False

        db_session.refresh(booking)
        assert booking.refund_status == models.RefundStatus.REFUND_INITIATED
        assert booking.status == models.BookingStatus.CANCELLED

    def test_partial_refund(self, client, db_session, admin_headers):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
            total_amount=5000.0,
        )
        txn = _create_transaction(
            db_session, booking,
            razorpay_payment_id="pay_refund_partial",
            status=models.TransactionStatus.SUCCESS,
        )

        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={
                    "booking_id": booking.id,
                    "amount": 2000.0,
                    "reason": "Partial refund for service issue",
                },
                headers=admin_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_partial"] is True
        assert data["refund_amount"] == 2000.0

    def test_refund_booking_not_found(self, client, db_session, admin_headers):
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={"booking_id": 99999},
                headers=admin_headers,
            )
        assert resp.status_code == 404

    def test_refund_unpaid_booking(self, client, db_session, admin_headers):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PENDING,
        )
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={"booking_id": booking.id},
                headers=admin_headers,
            )
        assert resp.status_code == 400

    def test_refund_no_razorpay_transaction(self, client, db_session, admin_headers):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        # No transaction at all
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={"booking_id": booking.id},
                headers=admin_headers,
            )
        assert resp.status_code == 404

    def test_refund_zero_amount(self, client, db_session, admin_headers):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        txn = _create_transaction(
            db_session, booking,
            razorpay_payment_id="pay_refund_zero",
            status=models.TransactionStatus.SUCCESS,
        )
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={"booking_id": booking.id, "amount": 0},
                headers=admin_headers,
            )
        assert resp.status_code == 422

    def test_refund_exceeds_total(self, client, db_session, admin_headers):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
            total_amount=1000.0,
        )
        txn = _create_transaction(
            db_session, booking,
            razorpay_payment_id="pay_refund_exceed",
            status=models.TransactionStatus.SUCCESS,
        )
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={"booking_id": booking.id, "amount": 5000.0},
                headers=admin_headers,
            )
        assert resp.status_code == 422

    def test_refund_api_failure(self, client, db_session, admin_headers):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        txn = _create_transaction(
            db_session, booking,
            razorpay_payment_id="pay_refund_apifail",
            status=models.TransactionStatus.SUCCESS,
        )
        with _mock_razorpay_client(fail_refund=True):
            resp = client.post(
                "/payments/razorpay/refund",
                json={"booking_id": booking.id},
                headers=admin_headers,
            )
        assert resp.status_code == 502

        db_session.refresh(booking)
        assert booking.refund_status == models.RefundStatus.REFUND_FAILED

    def test_refund_requires_admin(self, client, db_session):
        """Refund endpoint should require admin auth."""
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        resp = client.post(
            "/payments/razorpay/refund",
            json={"booking_id": booking.id},
        )
        assert resp.status_code in (401, 403)

    def test_refund_no_payment_id_on_transaction(self, client, db_session, admin_headers):
        """Transaction exists but has no razorpay_payment_id."""
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        # Transaction without payment_id
        txn = _create_transaction(
            db_session, booking,
            razorpay_payment_id=None,
            status=models.TransactionStatus.SUCCESS,
        )
        with _mock_razorpay_client():
            resp = client.post(
                "/payments/razorpay/refund",
                json={"booking_id": booking.id},
                headers=admin_headers,
            )
        assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# 9. Refund Timeline
# ──────────────────────────────────────────────────────────────────────────────


class TestRefundTimeline:
    def test_refund_timeline(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        now = _utc_now()
        booking.refund_status = models.RefundStatus.REFUND_INITIATED
        booking.refund_amount = 2500.0
        booking.refund_requested_at = now - timedelta(hours=1)
        booking.refund_initiated_at = now
        booking.refund_expected_settlement_at = now + timedelta(days=5)
        booking.refund_gateway_reference = "rfnd_timeline_001"
        db_session.commit()

        resp = client.get(f"/payments/razorpay/refund-timeline/{booking.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["refund_status"] == "refund_initiated"
        assert data["refund_amount"] == 2500.0
        assert data["gateway_reference"] == "rfnd_timeline_001"

    def test_refund_timeline_not_found(self, client, db_session):
        resp = client.get("/payments/razorpay/refund-timeline/99999")
        assert resp.status_code == 404

    def test_refund_timeline_no_refund(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        resp = client.get(f"/payments/razorpay/refund-timeline/{booking.id}")
        assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# 10. Payment Status (Polling / Multi-Tab Sync)
# ──────────────────────────────────────────────────────────────────────────────


class TestPaymentStatus:
    """Status polling endpoint used by frontend for multi-tab sync."""

    def test_status_pending(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["booking_status"] == "pending"
        assert data["payment_status"] == "pending"
        assert data["latest_transaction"] is None
        assert data["failed_payment_count"] == 0

    def test_status_with_transaction(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)
        txn = _create_transaction(db_session, booking, razorpay_order_id="order_status_001")

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["latest_transaction"] is not None
        assert data["latest_transaction"]["razorpay_order_id"] == "order_status_001"

    def test_status_confirmed(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        txn = _create_transaction(
            db_session, booking,
            status=models.TransactionStatus.SUCCESS,
            razorpay_payment_id="pay_status_confirmed",
        )

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["booking_status"] == "confirmed"
        assert data["payment_status"] == "paid"

    def test_status_not_found(self, client, db_session):
        resp = client.get("/payments/razorpay/status/99999")
        assert resp.status_code == 404

    def test_status_failed_count(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        # Create 5 failed transactions
        for i in range(5):
            _create_transaction(
                db_session, booking,
                razorpay_order_id=f"order_fail_{i}",
                status=models.TransactionStatus.FAILED,
            )

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["failed_payment_count"] == 5
        assert data["retry_after_seconds"] == 180  # cooldown after 5+ failures

    def test_status_hold_expired_flag(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            hold_expires_at=_utc_now() - timedelta(minutes=1),
        )

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        assert resp.status_code == 200
        assert resp.json()["hold_expired"] is True

    def test_status_hold_active(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            hold_expires_at=_utc_now() + timedelta(minutes=5),
        )

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        assert resp.status_code == 200
        assert resp.json()["hold_expired"] is False

    def test_status_with_refund(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(
            db_session, room,
            status=models.BookingStatus.CANCELLED,
            payment_status=models.PaymentStatus.REFUNDED,
        )
        booking.refund_status = models.RefundStatus.REFUND_SUCCESS
        db_session.commit()

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["refund_status"] == "refund_success"

    def test_status_retry_after_under_threshold(self, client, db_session):
        room = _create_room(db_session)
        booking = _create_booking(db_session, room)

        # Only 3 failed attempts (under 5 threshold)
        for i in range(3):
            _create_transaction(
                db_session, booking,
                razorpay_order_id=f"order_retry_{i}",
                status=models.TransactionStatus.FAILED,
            )

        resp = client.get(f"/payments/razorpay/status/{booking.id}")
        data = resp.json()
        assert data["failed_payment_count"] == 3
        assert data["retry_after_seconds"] == 0  # no cooldown yet


# ──────────────────────────────────────────────────────────────────────────────
# 11. Helper Function Unit Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestHelperFunctions:
    """Direct unit tests for helper functions."""

    def test_verify_razorpay_signature_valid(self):
        from routers.razorpay_payments import _verify_razorpay_signature

        order_id = "order_123"
        payment_id = "pay_456"
        secret = "my_secret"
        sig = _make_razorpay_signature(order_id, payment_id, secret)
        assert _verify_razorpay_signature(order_id, payment_id, sig, secret) is True

    def test_verify_razorpay_signature_invalid(self):
        from routers.razorpay_payments import _verify_razorpay_signature

        assert _verify_razorpay_signature("order", "pay", "bad_sig", "secret") is False

    def test_verify_webhook_signature_valid(self):
        from routers.razorpay_payments import _verify_webhook_signature

        body = b'{"event":"test"}'
        secret = "webhook_secret"
        sig = _make_webhook_signature(body, secret)
        assert _verify_webhook_signature(body, sig, secret) is True

    def test_verify_webhook_signature_invalid(self):
        from routers.razorpay_payments import _verify_webhook_signature

        assert _verify_webhook_signature(b"data", "bad_sig", "secret") is False

    def test_is_hold_expired_true(self):
        from routers.razorpay_payments import _is_hold_expired

        booking = MagicMock()
        booking.hold_expires_at = _utc_now() - timedelta(minutes=1)
        assert _is_hold_expired(booking) is True

    def test_is_hold_expired_false(self):
        from routers.razorpay_payments import _is_hold_expired

        booking = MagicMock()
        booking.hold_expires_at = _utc_now() + timedelta(minutes=10)
        assert _is_hold_expired(booking) is False

    def test_is_hold_expired_none(self):
        from routers.razorpay_payments import _is_hold_expired

        booking = MagicMock()
        booking.hold_expires_at = None
        assert _is_hold_expired(booking) is False

    def test_is_hold_expired_naive_datetime(self):
        """Should handle naive datetime (no tzinfo) gracefully."""
        from routers.razorpay_payments import _is_hold_expired

        booking = MagicMock()
        # Naive datetime in the past
        booking.hold_expires_at = datetime(2020, 1, 1, 0, 0, 0)
        assert _is_hold_expired(booking) is True

    def test_apply_paid_state(self):
        from routers.razorpay_payments import _apply_paid_state

        booking = MagicMock()
        _apply_paid_state(booking)
        assert booking.payment_status == models.PaymentStatus.PAID
        assert booking.status == models.BookingStatus.CONFIRMED

    def test_generate_txn_ref(self):
        from routers.razorpay_payments import _generate_txn_ref

        ref = _generate_txn_ref()
        assert ref.startswith("TXN-RZP-")
        assert len(ref) > 8

    def test_utc_now(self):
        from routers.razorpay_payments import utc_now

        now = utc_now()
        assert now.tzinfo is not None
