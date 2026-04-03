from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import models


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_notification(
    db,
    *,
    event_type: str,
    recipient_email: str,
    subject: str,
    body: str,
    booking_id: Optional[int] = None,
    transaction_id: Optional[int] = None,
) -> models.NotificationOutbox:
    notification = models.NotificationOutbox(
        booking_id=booking_id,
        transaction_id=transaction_id,
        event_type=event_type,
        recipient_email=recipient_email,
        subject=subject,
        body=body,
        status=models.NotificationStatus.PENDING,
    )
    db.add(notification)
    db.flush()
    return notification


def queue_booking_hold_email(db, booking: models.Booking) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="booking_hold_created",
        recipient_email=booking.email,
        booking_id=booking.id,
        subject=f"Your reservation hold is ready: {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name}, your reservation hold for booking "
            f"{booking.booking_ref} is active until {booking.hold_expires_at}."
        ),
    )


def queue_booking_confirmation_email(
    db, booking: models.Booking, transaction: models.Transaction
) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="booking_confirmed",
        recipient_email=booking.email,
        booking_id=booking.id,
        transaction_id=transaction.id,
        subject=f"Booking confirmed: {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name}, your booking {booking.booking_ref} is confirmed. "
            f"Total paid: ${booking.total_amount:.2f}."
        ),
    )


def queue_payment_receipt_email(
    db, booking: models.Booking, transaction: models.Transaction
) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="payment_receipt",
        recipient_email=booking.email,
        booking_id=booking.id,
        transaction_id=transaction.id,
        subject=f"Payment receipt for {booking.booking_ref}",
        body=(
            f"Payment received for booking {booking.booking_ref}. "
            f"Transaction ref: {transaction.transaction_ref}."
        ),
    )


def queue_payment_failure_email(
    db,
    booking: models.Booking,
    transaction: models.Transaction,
    reason: str,
) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="payment_failed_retry",
        recipient_email=booking.email,
        booking_id=booking.id,
        transaction_id=transaction.id,
        subject=f"Payment failed for {booking.booking_ref}",
        body=(
            f"Your payment for booking {booking.booking_ref} failed: {reason}. "
            f"Please retry payment to keep your reservation active."
        ),
    )


def queue_booking_cancellation_email(
    db, booking: models.Booking
) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="booking_cancelled",
        recipient_email=booking.email,
        booking_id=booking.id,
        subject=f"Booking cancelled: {booking.booking_ref}",
        body=f"Your booking {booking.booking_ref} has been cancelled.",
    )


def deliver_notification(notification: models.NotificationOutbox) -> None:
    if notification.recipient_email.startswith("fail-delivery+"):
        raise ValueError("Notification delivery rejected for invalid test domain")


def process_pending_notifications(db, limit: int = 25) -> dict[str, int]:
    notifications = (
        db.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.status == models.NotificationStatus.PENDING)
        .order_by(models.NotificationOutbox.created_at.asc(), models.NotificationOutbox.id.asc())
        .limit(limit)
        .all()
    )

    sent = 0
    failed = 0
    for notification in notifications:
        try:
            deliver_notification(notification)
            notification.status = models.NotificationStatus.SENT
            notification.sent_at = utc_now()
            notification.failure_reason = None
            sent += 1
        except Exception as exc:
            notification.status = models.NotificationStatus.FAILED
            notification.failure_reason = str(exc)
            failed += 1

    if notifications:
        db.commit()

    return {
        "processed": len(notifications),
        "sent": sent,
        "failed": failed,
    }
