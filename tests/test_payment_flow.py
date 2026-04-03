import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_bootstrap.db")

import models  # noqa: E402
from database import Base, get_db  # noqa: E402
from routers import bookings, payments  # noqa: E402


class PaymentFlowTests(unittest.TestCase):
    def setUp(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_file.close()
        self.engine = create_engine(
            f"sqlite:///{self.db_file.name}",
            connect_args={"check_same_thread": False},
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

        self.app = FastAPI()
        self.app.include_router(bookings.router)
        self.app.include_router(payments.router)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(self.app)

        with self.SessionLocal() as db:
            room = models.Room(
                hotel_name="Test Hotel",
                room_type=models.RoomType.DELUXE,
                description="Test room",
                price=200.0,
                availability=True,
                city="Test City",
                country="Test Country",
            )
            db.add(room)
            db.commit()
            db.refresh(room)
            self.room_id = room.id

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()
        Path(self.db_file.name).unlink(missing_ok=True)

    def create_booking(self):
        response = self.client.post(
            "/bookings",
            json={
                "user_name": "Athit",
                "email": "athit@example.com",
                "phone": "1234567890",
                "room_id": self.room_id,
                "check_in": datetime.now(timezone.utc).isoformat(),
                "check_out": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                "guests": 2,
                "special_requests": "",
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def payment_success(self, booking_id: int, ref: str, payment_intent_id: str | None = None):
        return self.client.post(
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

    def test_create_payment_intent_blocks_paid_booking(self):
        booking = self.create_booking()
        success = self.payment_success(booking["id"], "TXN-PAID001", "pi_paid_001")
        self.assertEqual(success.status_code, 200)

        with patch("routers.payments.stripe.PaymentIntent.create"):
            response = self.client.post(
                "/payments/create-payment-intent",
                json={"booking_id": booking["id"], "payment_method": "card"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "Booking already paid")

    def test_payment_success_is_idempotent(self):
        booking = self.create_booking()

        first = self.payment_success(booking["id"], "TXN-IDEMP001", "pi_idemp_001")
        second = self.payment_success(booking["id"], "TXN-IDEMP002", "pi_idemp_001")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])

        with self.SessionLocal() as db:
            txn_count = db.query(models.Transaction).count()
            paid_booking = db.query(models.Booking).filter_by(id=booking["id"]).first()
            self.assertEqual(txn_count, 1)
            self.assertEqual(paid_booking.payment_status, models.PaymentStatus.PAID)

    def test_failed_payment_can_retry_and_then_succeed(self):
        booking = self.create_booking()

        fail = self.client.post(
            "/payments/payment-failure",
            params={"booking_id": booking["id"], "reason": "Card declined"},
        )
        success = self.payment_success(booking["id"], "TXN-RETRY001", "pi_retry_001")

        self.assertEqual(fail.status_code, 200)
        self.assertEqual(success.status_code, 200)

        with self.SessionLocal() as db:
            txns = (
                db.query(models.Transaction)
                .filter(models.Transaction.booking_id == booking["id"])
                .order_by(models.Transaction.id.asc())
                .all()
            )
            booking_row = db.query(models.Booking).filter_by(id=booking["id"]).first()
            self.assertEqual(len(txns), 2)
            self.assertEqual(txns[0].status, models.TransactionStatus.FAILED)
            self.assertEqual(txns[1].status, models.TransactionStatus.SUCCESS)
            self.assertEqual(booking_row.payment_status, models.PaymentStatus.PAID)
            self.assertEqual(booking_row.status, models.BookingStatus.CONFIRMED)

    def test_payment_failure_rejected_when_booking_is_already_paid(self):
        booking = self.create_booking()
        success = self.payment_success(booking["id"], "TXN-LOCK001", "pi_lock_001")
        self.assertEqual(success.status_code, 200)

        fail = self.client.post(
            "/payments/payment-failure",
            params={"booking_id": booking["id"], "reason": "Should not record"},
        )
        self.assertEqual(fail.status_code, 409)

    def test_payment_status_returns_latest_transaction(self):
        booking = self.create_booking()
        self.client.post(
            "/payments/payment-failure",
            params={"booking_id": booking["id"], "reason": "Retry later"},
        )

        response = self.client.get(f"/payments/status/{booking['id']}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["booking_id"], booking["id"])
        self.assertEqual(body["payment_status"], "failed")
        self.assertEqual(body["latest_transaction"]["status"], "failed")

    def test_webhook_success_is_idempotent(self):
        booking = self.create_booking()
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
            first = self.client.post("/payments/webhook", data=b"{}", headers={"stripe-signature": "sig"})
            second = self.client.post("/payments/webhook", data=b"{}", headers={"stripe-signature": "sig"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        with self.SessionLocal() as db:
            txns = db.query(models.Transaction).filter_by(booking_id=booking["id"]).all()
            booking_row = db.query(models.Booking).filter_by(id=booking["id"]).first()
            self.assertEqual(len(txns), 1)
            self.assertEqual(txns[0].status, models.TransactionStatus.SUCCESS)
            self.assertEqual(booking_row.payment_status, models.PaymentStatus.PAID)

    def test_webhook_failure_marks_booking_failed(self):
        booking = self.create_booking()
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
            response = self.client.post("/payments/webhook", data=b"{}", headers={"stripe-signature": "sig"})

        self.assertEqual(response.status_code, 200)

        with self.SessionLocal() as db:
            txns = db.query(models.Transaction).filter_by(booking_id=booking["id"]).all()
            booking_row = db.query(models.Booking).filter_by(id=booking["id"]).first()
            self.assertEqual(len(txns), 1)
            self.assertEqual(txns[0].status, models.TransactionStatus.FAILED)
            self.assertEqual(txns[0].failure_reason, "Insufficient funds")
            self.assertEqual(booking_row.payment_status, models.PaymentStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
