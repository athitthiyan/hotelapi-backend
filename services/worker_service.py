from sqlalchemy import text
from sqlalchemy.orm import Session

from routers.payments import reconcile_stuck_payments
from services.notification_service import process_pending_notifications


def run_maintenance_cycle(
    db: Session,
    payment_timeout_minutes: int = 30,
    notification_limit: int = 50,
) -> dict[str, int]:
    reconciled_payments = reconcile_stuck_payments(
        db, timeout_minutes=payment_timeout_minutes
    )
    notification_result = process_pending_notifications(db, limit=notification_limit)
    return {
        "reconciled_payments": reconciled_payments,
        "processed_notifications": notification_result["total"],
        "sent_notifications": notification_result["sent"],
        "failed_notifications": notification_result["failed"],
    }


def get_operational_counts(db: Session) -> dict[str, int]:
    pending_notifications = db.execute(
        text(
            """
            SELECT count(*)
            FROM notification_outbox
            WHERE CAST(status AS TEXT) = 'pending'
            """
        )
    ).scalar_one()
    processing_payments = db.execute(
        text(
            """
            SELECT count(*)
            FROM transactions
            WHERE CAST(status AS TEXT) IN ('pending', 'processing')
            """
        )
    ).scalar_one()
    return {
        "pending_notifications": pending_notifications,
        "processing_payments": processing_payments,
    }
