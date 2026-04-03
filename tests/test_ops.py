from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

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


def test_ready_reports_connected_database_and_operational_counts(
    client, create_booking, db_session
):
    booking = create_booking()
    transaction = models.Transaction(
        booking_id=booking["id"],
        transaction_ref="TXN-OPS-001",
        amount=booking["total_amount"],
        currency="USD",
        payment_method="card",
        status=models.TransactionStatus.PROCESSING,
    )
    notification = models.NotificationOutbox(
        booking_id=booking["id"],
        event_type="booking_hold_created",
        recipient_email=booking["email"],
        subject="Hold",
        body="Hold queued",
        status=models.NotificationStatus.PENDING,
    )
    db_session.add_all([transaction, notification])
    db_session.commit()

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "hotel-api",
        "database": "connected",
        "pending_notifications": 2,
        "processing_payments": 1,
    }


def test_ready_reports_degraded_when_database_is_unavailable(client):
    with patch("routers.ops.engine.connect", side_effect=SQLAlchemyError("boom")):
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "unavailable"


def test_admin_can_run_maintenance_cycle(client, create_booking, db_session):
    headers = admin_headers(client, db_session)
    booking = create_booking()

    transaction = models.Transaction(
        booking_id=booking["id"],
        transaction_ref="TXN-OPS-002",
        amount=booking["total_amount"],
        currency="USD",
        payment_method="card",
        status=models.TransactionStatus.PROCESSING,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    notification = models.NotificationOutbox(
        booking_id=booking["id"],
        event_type="payment_failed_retry",
        recipient_email=booking["email"],
        subject="Retry",
        body="Retry payment",
        status=models.NotificationStatus.PENDING,
    )
    db_session.add_all([transaction, notification])
    db_session.commit()

    response = client.post(
        "/ops/run-maintenance",
        headers=headers,
        params={"payment_timeout_minutes": 30, "notification_limit": 10},
    )

    db_session.refresh(transaction)
    db_session.refresh(notification)

    assert response.status_code == 200
    assert response.json()["reconciled_payments"] == 1
    assert response.json()["processed_notifications"] == 2
    assert response.json()["sent_notifications"] == 2
    assert response.json()["failed_notifications"] == 0
    assert transaction.status == models.TransactionStatus.EXPIRED
    assert notification.status == models.NotificationStatus.SENT

    audit_logs = client.get(
        "/ops/audit-logs",
        headers=headers,
        params={"action": "ops.maintenance.run"},
    )

    assert audit_logs.status_code == 200
    assert audit_logs.json()["total"] == 1
    assert audit_logs.json()["logs"][0]["action"] == "ops.maintenance.run"


def test_non_admin_cannot_run_maintenance_cycle(client, db_session):
    user = models.User(
        email="user-ops@example.com",
        full_name="User Ops",
        hashed_password=hash_password("UserPass123"),
        is_admin=False,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "user-ops@example.com", "password": "UserPass123"},
    )

    response = client.post(
        "/ops/run-maintenance",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 403


def test_non_admin_cannot_view_audit_logs(client, db_session):
    user = models.User(
        email="user-audit@example.com",
        full_name="User Audit",
        hashed_password=hash_password("UserPass123"),
        is_admin=False,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "user-audit@example.com", "password": "UserPass123"},
    )

    response = client.get(
        "/ops/audit-logs",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 403
