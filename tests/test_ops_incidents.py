from datetime import timedelta, timezone, datetime
import json
import importlib

from sqlalchemy.exc import SQLAlchemyError

import models
from routers import ops
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


def test_readiness_check_reports_degraded_when_operational_counts_fail(client, monkeypatch):
    def raise_counts_error(_db):
        raise SQLAlchemyError("counts unavailable")

    monkeypatch.setattr(ops, "get_operational_counts", raise_counts_error)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["pending_notifications"] == -1
    assert response.json()["processing_payments"] == -1


def test_run_maintenance_endpoint_records_audit_log(client, db_session, monkeypatch):
    headers = admin_headers(client, db_session)

    def fake_maintenance_cycle(db, payment_timeout_minutes, notification_limit):
        assert db is not None
        assert payment_timeout_minutes == 15
        assert notification_limit == 5
        return {
            "reconciled_payments": 0,
            "processed_notifications": 1,
            "sent_notifications": 1,
            "failed_notifications": 0,
        }

    monkeypatch.setattr(ops, "run_maintenance_cycle", fake_maintenance_cycle)

    response = client.post(
        "/ops/run-maintenance",
        headers=headers,
        params={"payment_timeout_minutes": 15, "notification_limit": 5},
    )
    audit_log = db_session.query(models.AuditLog).filter_by(action="ops.maintenance.run").first()

    assert response.status_code == 200
    assert response.json()["sent_notifications"] == 1
    assert audit_log is not None
    assert json.loads(audit_log.metadata_json)["processed_notifications"] == 1


def test_key_rotation_status_requires_admin_and_returns_report(client, db_session, monkeypatch):
    headers = admin_headers(client, db_session)
    monkeypatch.setattr(
        "services.key_rotation_service.get_rotation_report",
        lambda: {"status": "ok", "keys": []},
    )

    unauthenticated = client.get("/ops/key-rotation-status")
    authenticated = client.get("/ops/key-rotation-status", headers=headers)

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {"status": "ok", "keys": []}


def test_test_email_endpoint_covers_missing_success_and_failure_paths(client, db_session, monkeypatch):
    headers = admin_headers(client, db_session)
    live_database = importlib.import_module("database")
    monkeypatch.setattr(live_database.settings, "resend_api_key", "")

    missing_key = client.post("/ops/test-email", headers=headers)

    sent_messages = []

    def fake_send(notification, api_key, from_addr):
        sent_messages.append((notification.recipient_email, api_key, from_addr))

    monkeypatch.setattr(live_database.settings, "resend_api_key", "re_test")
    monkeypatch.setattr(live_database.settings, "email_from_name", "Stayvora")
    monkeypatch.setattr(live_database.settings, "email_from_address", "noreply@stayvora.co.in")
    monkeypatch.setattr("services.notification_service._send_via_resend", fake_send)

    success = client.post("/ops/test-email", headers=headers)
    audit_log = db_session.query(models.AuditLog).filter_by(action="ops.email.test_sent").first()

    def failing_send(_notification, _api_key, _from_addr):
        raise RuntimeError("provider down")

    monkeypatch.setattr("services.notification_service._send_via_resend", failing_send)
    failed = client.post("/ops/test-email", headers=headers)

    assert missing_key.status_code == 503
    assert success.status_code == 200
    assert success.json()["status"] == "sent"
    assert sent_messages == [("admin-ops@example.com", "re_test", "Stayvora <noreply@stayvora.co.in>")]
    assert audit_log is not None
    assert failed.status_code == 502
    assert "provider down" in failed.json()["detail"]
