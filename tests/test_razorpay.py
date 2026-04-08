"""Tests for Razorpay payment endpoints."""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import models
from tests.conftest import *  # noqa: F401,F403


def _booking_payload(room_id):
    return {
        "user_name": "Razorpay Tester",
        "email": "rzp@example.com",
        "phone": "9876543210",
        "room_id": room_id,
        "check_in": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "check_out": (datetime.now(timezone.utc) + timedelta(days=2, hours=2)).isoformat(),
        "guests": 2,
        "special_requests": "",
    }


def _create_booking(client, room_id):
    resp = client.post("/bookings", json=_booking_payload(room_id))
    assert resp.status_code == 201
    return resp.json()


# ── helpers ──────────────────────────────────────────────────────────────────


def _sign(order_id: str, payment_id: str, secret: str) -> str:
    payload = f"{order_id}|{payment_id}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ── create-order ─────────────────────────────────────────────────────────────


class TestCreateOrder:
    def test_create_mock_order(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "mock"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"].startswith("order_mock_")
        assert data["currency"] == "INR"
        assert data["idempotent"] is False

    def test_create_order_booking_not_found(self, client, room_id):
        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": 99999, "payment_method": "upi"},
        )
        assert resp.status_code == 404

    def test_create_order_already_paid(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        booking = db_session.get(models.Booking, bk["id"])
        booking.payment_status = models.PaymentStatus.PAID
        db_session.commit()

        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "upi"},
        )
        assert resp.status_code == 409

    def test_create_order_cancelled_booking(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        booking = db_session.get(models.Booking, bk["id"])
        booking.status = models.BookingStatus.CANCELLED
        db_session.commit()

        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "upi"},
        )
        assert resp.status_code == 400

    def test_create_order_expired_booking(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        booking = db_session.get(models.Booking, bk["id"])
        booking.status = models.BookingStatus.EXPIRED
        db_session.commit()

        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "upi"},
        )
        assert resp.status_code == 400

    def test_create_order_invalid_method(self, client, room_id):
        bk = _create_booking(client, room_id)
        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "bitcoin"},
        )
        assert resp.status_code == 422

    @patch("routers.razorpay_payments._get_razorpay_client")
    def test_create_order_normalises_phonepay(self, mock_client_fn, client, room_id, db_session):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {"id": "order_live_phonepe"}
        mock_client_fn.return_value = mock_client

        bk = _create_booking(client, room_id)
        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "phonepay"},
        )
        assert resp.status_code == 200
        assert resp.json()["order_id"] == "order_live_phonepe"
        txn = db_session.query(models.Transaction).filter_by(
            booking_id=bk["id"]
        ).first()
        assert txn.payment_method == "phonepe"

    def test_idempotency_returns_existing(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        idem_key = f"idem-{secrets.token_hex(4)}"

        r1 = client.post(
            "/payments/razorpay/create-order",
            json={
                "booking_id": bk["id"],
                "payment_method": "mock",
                "idempotency_key": idem_key,
            },
        )
        assert r1.status_code == 200
        assert r1.json()["idempotent"] is False

        r2 = client.post(
            "/payments/razorpay/create-order",
            json={
                "booking_id": bk["id"],
                "payment_method": "mock",
                "idempotency_key": idem_key,
            },
        )
        assert r2.status_code == 200
        assert r2.json()["idempotent"] is True
        assert r2.json()["order_id"] == r1.json()["order_id"]

    @patch("routers.razorpay_payments._get_razorpay_client")
    def test_create_order_with_real_method(self, mock_client_fn, client, room_id):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {"id": "order_live_abc"}
        mock_client_fn.return_value = mock_client

        bk = _create_booking(client, room_id)
        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "upi"},
        )
        assert resp.status_code == 200
        assert resp.json()["order_id"] == "order_live_abc"
        mock_client.order.create.assert_called_once()

    @patch("routers.razorpay_payments._get_razorpay_client")
    def test_create_order_razorpay_api_failure(self, mock_client_fn, client, room_id):
        mock_client = MagicMock()
        mock_client.order.create.side_effect = Exception("Razorpay API down")
        mock_client_fn.return_value = mock_client

        bk = _create_booking(client, room_id)
        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "card"},
        )
        assert resp.status_code == 502


# ── verify-payment ───────────────────────────────────────────────────────────


class TestVerifyPayment:
    def _setup_pending_txn(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "mock"},
        )
        order = resp.json()
        return bk, order

    def test_transaction_not_found(self, client, room_id):
        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": "order_x",
                "razorpay_payment_id": "pay_x",
                "razorpay_signature": "sig_x",
                "transaction_ref": "TXN-NONEXISTENT",
            },
        )
        assert resp.status_code == 404

    def test_verify_rejects_when_no_key_secret(self, client, room_id, db_session):
        bk, order = self._setup_pending_txn(client, room_id, db_session)

        with patch("routers.razorpay_payments.settings") as mock_settings:
            mock_settings.razorpay_key_id = "rzp_test"
            mock_settings.razorpay_key_secret = ""
            mock_settings.razorpay_webhook_secret = ""

            resp = client.post(
                "/payments/razorpay/verify-payment",
                json={
                    "razorpay_order_id": order["order_id"],
                    "razorpay_payment_id": "pay_xyz",
                    "razorpay_signature": "sig_xyz",
                    "transaction_ref": order["transaction_ref"],
                },
            )
        assert resp.status_code == 503

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_bad_signature(self, mock_settings, mock_client_fn, client, room_id, db_session):
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = "test_secret"
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order["order_id"],
                "razorpay_payment_id": "pay_xyz",
                "razorpay_signature": "bad_sig",
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_payment_captured_success(self, mock_settings, mock_client_fn, client, room_id, db_session):
        secret = "test_secret_123"
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = secret
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        order_id = order["order_id"]
        payment_id = "pay_captured_001"
        sig = _sign(order_id, payment_id, secret)

        booking = db_session.get(models.Booking, bk["id"])
        amount_paise = int(booking.total_amount * 100)

        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {
            "status": "captured",
            "order_id": order_id,
            "amount": amount_paise,
        }
        mock_client_fn.return_value = mock_client

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        db_session.expire_all()
        booking = db_session.get(models.Booking, bk["id"])
        assert booking.payment_status == models.PaymentStatus.PAID
        assert booking.status == models.BookingStatus.CONFIRMED

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_payment_authorized_captures(self, mock_settings, mock_client_fn, client, room_id, db_session):
        secret = "test_secret_123"
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = secret
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        order_id = order["order_id"]
        payment_id = "pay_auth_001"
        sig = _sign(order_id, payment_id, secret)

        booking = db_session.get(models.Booking, bk["id"])
        amount_paise = int(booking.total_amount * 100)

        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {
            "status": "authorized",
            "order_id": order_id,
            "amount": amount_paise,
        }
        mock_client_fn.return_value = mock_client

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 200
        mock_client.payment.capture.assert_called_once_with(payment_id, amount_paise)

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_payment_capture_fails(self, mock_settings, mock_client_fn, client, room_id, db_session):
        secret = "test_secret_123"
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = secret
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        order_id = order["order_id"]
        payment_id = "pay_capfail"
        sig = _sign(order_id, payment_id, secret)

        booking = db_session.get(models.Booking, bk["id"])
        amount_paise = int(booking.total_amount * 100)

        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {
            "status": "authorized",
            "order_id": order_id,
            "amount": amount_paise,
        }
        mock_client.payment.capture.side_effect = Exception("Capture failed")
        mock_client_fn.return_value = mock_client

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 400
        assert "capture failed" in resp.json()["detail"].lower()

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_payment_not_captured_not_authorized(self, mock_settings, mock_client_fn, client, room_id, db_session):
        secret = "test_secret_123"
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = secret
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        order_id = order["order_id"]
        payment_id = "pay_failed"
        sig = _sign(order_id, payment_id, secret)

        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {
            "status": "failed",
            "order_id": order_id,
            "amount": int(db_session.get(models.Booking, bk["id"]).total_amount * 100),
        }
        mock_client_fn.return_value = mock_client

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 400

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_payment_order_id_mismatch(self, mock_settings, mock_client_fn, client, room_id, db_session):
        secret = "test_secret_123"
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = secret
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        order_id = order["order_id"]
        payment_id = "pay_mismatch"
        sig = _sign(order_id, payment_id, secret)

        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {
            "status": "captured",
            "order_id": "order_DIFFERENT",
            "amount": int(db_session.get(models.Booking, bk["id"]).total_amount * 100),
        }
        mock_client_fn.return_value = mock_client

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 400
        assert "mismatch" in resp.json()["detail"].lower()

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_payment_amount_mismatch(self, mock_settings, mock_client_fn, client, room_id, db_session):
        secret = "test_secret_123"
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = secret
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        order_id = order["order_id"]
        payment_id = "pay_wrong_amt"
        sig = _sign(order_id, payment_id, secret)

        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {
            "status": "captured",
            "order_id": order_id,
            "amount": 100,  # wrong amount
        }
        mock_client_fn.return_value = mock_client

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 400
        assert "amount" in resp.json()["detail"].lower()

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_fetch_api_error(self, mock_settings, mock_client_fn, client, room_id, db_session):
        secret = "test_secret_123"
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = secret
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        order_id = order["order_id"]
        payment_id = "pay_err"
        sig = _sign(order_id, payment_id, secret)

        mock_client = MagicMock()
        mock_client.payment.fetch.side_effect = Exception("API timeout")
        mock_client_fn.return_value = mock_client

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 502

    @patch("routers.razorpay_payments._get_razorpay_client")
    @patch("routers.razorpay_payments.settings")
    def test_verify_already_success_returns_early(self, mock_settings, mock_client_fn, client, room_id, db_session):
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = "test_secret"
        mock_settings.razorpay_webhook_secret = ""

        bk, order = self._setup_pending_txn(client, room_id, db_session)
        txn = db_session.query(models.Transaction).filter_by(
            transaction_ref=order["transaction_ref"]
        ).first()
        txn.status = models.TransactionStatus.SUCCESS
        db_session.commit()

        resp = client.post(
            "/payments/razorpay/verify-payment",
            json={
                "razorpay_order_id": order["order_id"],
                "razorpay_payment_id": "pay_dup",
                "razorpay_signature": "any",
                "transaction_ref": order["transaction_ref"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Payment already confirmed"


# ── webhook ──────────────────────────────────────────────────────────────────


class TestWebhook:
    def test_webhook_payment_captured(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        order_resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "mock"},
        ).json()

        event = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_wh_001",
                        "order_id": order_resp["order_id"],
                        "notes": {"booking_id": str(bk["id"])},
                    }
                }
            },
        }
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200

        db_session.expire_all()
        booking = db_session.get(models.Booking, bk["id"])
        assert booking.payment_status == models.PaymentStatus.PAID
        assert booking.status == models.BookingStatus.CONFIRMED

    def test_webhook_payment_failed(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        order_resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "mock"},
        ).json()

        event = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_wh_fail",
                        "order_id": order_resp["order_id"],
                        "error_description": "Insufficient funds",
                    }
                }
            },
        }
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200

        db_session.expire_all()
        txn = db_session.query(models.Transaction).filter_by(
            razorpay_order_id=order_resp["order_id"]
        ).first()
        assert txn.status == models.TransactionStatus.FAILED
        booking = db_session.get(models.Booking, bk["id"])
        assert booking.payment_status == models.PaymentStatus.FAILED

    def test_webhook_unknown_event(self, client):
        event = {"event": "order.paid", "payload": {}}
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_webhook_invalid_json(self, client):
        resp = client.post(
            "/payments/razorpay/webhook",
            content=b"not json",
        )
        assert resp.status_code == 400

    @patch("routers.razorpay_payments.settings")
    def test_webhook_signature_valid(self, mock_settings, client, room_id):
        wh_secret = "webhook_secret_abc"
        mock_settings.razorpay_webhook_secret = wh_secret
        mock_settings.razorpay_key_id = "rzp_test"
        mock_settings.razorpay_key_secret = "secret"

        body = json.dumps({"event": "ping", "payload": {}}).encode()
        sig = hmac.new(wh_secret.encode(), body, hashlib.sha256).hexdigest()

        resp = client.post(
            "/payments/razorpay/webhook",
            content=body,
            headers={"x-razorpay-signature": sig},
        )
        assert resp.status_code == 200

    @patch("routers.razorpay_payments.settings")
    def test_webhook_signature_invalid(self, mock_settings, client):
        wh_secret = "webhook_secret_abc"
        mock_settings.razorpay_webhook_secret = wh_secret

        body = json.dumps({"event": "ping", "payload": {}}).encode()

        resp = client.post(
            "/payments/razorpay/webhook",
            content=body,
            headers={"x-razorpay-signature": "bad_sig"},
        )
        assert resp.status_code == 400

    def test_webhook_captured_idempotent_already_success(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        order_resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "mock"},
        ).json()

        txn = db_session.query(models.Transaction).filter_by(
            razorpay_order_id=order_resp["order_id"]
        ).first()
        txn.status = models.TransactionStatus.SUCCESS
        db_session.commit()

        event = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_dup",
                        "order_id": order_resp["order_id"],
                        "notes": {},
                    }
                }
            },
        }
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200  # no-op, still ok

    def test_webhook_captured_no_matching_order(self, client):
        event = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_orphan",
                        "order_id": "order_UNKNOWN",
                        "notes": {},
                    }
                }
            },
        }
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200

    def test_webhook_failed_no_matching_order(self, client):
        event = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_orphan",
                        "order_id": "order_UNKNOWN",
                        "error_description": "fail",
                    }
                }
            },
        }
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200

    def test_webhook_failed_already_not_pending(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        order_resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "mock"},
        ).json()

        txn = db_session.query(models.Transaction).filter_by(
            razorpay_order_id=order_resp["order_id"]
        ).first()
        txn.status = models.TransactionStatus.SUCCESS
        db_session.commit()

        event = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_late_fail",
                        "order_id": order_resp["order_id"],
                    }
                }
            },
        }
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200

    def test_webhook_captured_booking_already_paid(self, client, room_id, db_session):
        bk = _create_booking(client, room_id)
        order_resp = client.post(
            "/payments/razorpay/create-order",
            json={"booking_id": bk["id"], "payment_method": "mock"},
        ).json()

        booking = db_session.get(models.Booking, bk["id"])
        booking.payment_status = models.PaymentStatus.PAID
        db_session.commit()

        event = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_dup2",
                        "order_id": order_resp["order_id"],
                        "notes": {},
                    }
                }
            },
        }
        resp = client.post("/payments/razorpay/webhook", content=json.dumps(event))
        assert resp.status_code == 200
