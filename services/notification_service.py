"""
Notification service — outbox queue + real delivery via Resend.

Delivery flow:
  1. Business logic calls queue_*_email() -> writes a row to notification_outbox (PENDING).
  2. APScheduler (every 2 min) calls process_pending_notifications() ->
     iterates PENDING rows -> calls deliver_notification() -> marks SENT/FAILED.
  3. deliver_notification() calls Resend API when RESEND_API_KEY is set.
     Without a key it logs and marks SENT (dev/test mode).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import models
from database import get_settings

logger = logging.getLogger(__name__)


# ---- helpers -----------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---- core enqueue ------------------------------------------------------------

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


# ---- typed queue helpers -----------------------------------------------------

def queue_booking_hold_email(db, booking: models.Booking) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="booking_hold_created",
        recipient_email=booking.email,
        booking_id=booking.id,
        subject=f"Your reservation hold is ready — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"Your reservation hold for booking {booking.booking_ref} is active.\n"
            f"Hold expires: {booking.hold_expires_at} UTC.\n\n"
            "Complete payment before the timer runs out to confirm your stay.\n\n"
            "— Stayvora Team"
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
        subject=f"Booking confirmed — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"Your booking is CONFIRMED.\n\n"
            f"Booking ref  : {booking.booking_ref}\n"
            f"Transaction  : {transaction.transaction_ref}\n"
            f"Check-in     : {booking.check_in}\n"
            f"Check-out    : {booking.check_out}\n"
            f"Guests       : {booking.guests}\n"
            f"Total paid   : ${booking.total_amount:.2f}\n\n"
            "Your invoice is available in the Stayvora app.\n\n"
            "— Stayvora Team"
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
        subject=f"Payment receipt — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"We received your payment of ${transaction.amount:.2f}.\n\n"
            f"Booking ref     : {booking.booking_ref}\n"
            f"Transaction ref : {transaction.transaction_ref}\n"
            f"Payment method  : {transaction.payment_method or 'Card'}\n\n"
            "— Stayvora Team"
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
        subject=f"Payment failed — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"Your payment for booking {booking.booking_ref} failed.\n"
            f"Reason: {reason}\n\n"
            "Your reservation hold is still active. Please retry to keep your booking.\n\n"
            "— Stayvora Team"
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
        subject=f"Booking cancelled — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"Your booking {booking.booking_ref} has been cancelled.\n\n"
            "If you did not request this, please contact support immediately.\n\n"
            "— Stayvora Team"
        ),
    )


def queue_refund_initiated_email(db, booking: models.Booking) -> models.NotificationOutbox:
    settlement = (
        str(booking.refund_expected_settlement_at)
        if booking.refund_expected_settlement_at
        else "5-7 business days"
    )
    return enqueue_notification(
        db,
        event_type="refund_initiated",
        recipient_email=booking.email,
        booking_id=booking.id,
        subject=f"Refund started — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"Your refund for booking {booking.booking_ref} has been initiated.\n\n"
            f"Refund amount : ${booking.refund_amount:.2f}\n"
            f"Expected by   : {settlement}\n\n"
            "You will receive another email once the refund completes.\n\n"
            "— Stayvora Team"
        ),
    )


def queue_refund_success_email(db, booking: models.Booking) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="refund_success",
        recipient_email=booking.email,
        booking_id=booking.id,
        subject=f"Refund complete — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"Your refund of ${booking.refund_amount:.2f} for booking "
            f"{booking.booking_ref} has been processed.\n\n"
            f"Gateway reference: {booking.refund_gateway_reference or 'N/A'}\n\n"
            "Please allow 2-5 business days for the amount to appear in your account.\n\n"
            "— Stayvora Team"
        ),
    )


def queue_refund_failure_email(db, booking: models.Booking) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="refund_failed",
        recipient_email=booking.email,
        booking_id=booking.id,
        subject=f"Refund issue — {booking.booking_ref}",
        body=(
            f"Hi {booking.user_name},\n\n"
            f"We encountered an issue processing your refund for booking {booking.booking_ref}.\n\n"
            f"Reason: {booking.refund_failed_reason or 'Processing error'}\n\n"
            "Our support team has been notified and will contact you within 24 hours.\n\n"
            "— Stayvora Team"
        ),
    )


def queue_admin_alert_email(
    db,
    *,
    recipient_email: str,
    subject: str,
    body: str,
    booking_id: Optional[int] = None,
    transaction_id: Optional[int] = None,
    event_type: str = "admin_alert",
) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type=event_type,
        recipient_email=recipient_email,
        booking_id=booking_id,
        transaction_id=transaction_id,
        subject=subject,
        body=body,
    )


def queue_booking_support_request_email(
    db,
    *,
    recipient_email: str,
    booking: models.Booking,
    category: str,
    message: str,
) -> models.NotificationOutbox:
    return enqueue_notification(
        db,
        event_type="booking_support_request",
        recipient_email=recipient_email,
        booking_id=booking.id,
        subject=f"Support request — {booking.booking_ref}",
        body=(
            f"Category   : {category}\n"
            f"Booking ref: {booking.booking_ref}\n"
            f"Guest      : {booking.user_name} <{booking.email}>\n\n"
            f"Message:\n{message}"
        ),
    )


# ---- delivery driver ---------------------------------------------------------

def _send_via_resend(
    notification: models.NotificationOutbox,
    api_key: str,
    from_addr: str,
) -> None:
    """Send email via Resend API. Raises RuntimeError/Exception on failure."""
    try:
        import resend  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "resend package is not installed. Run: pip install resend"
        ) from exc

    resend.api_key = api_key
    params = {
        "from": from_addr,
        "to": [notification.recipient_email],
        "subject": notification.subject,
        "text": notification.body,
    }
    resend.Emails.send(params)  # type: ignore[attr-defined]


def deliver_notification(notification: models.NotificationOutbox) -> None:
    """
    Attempt real delivery of a single notification.

    - RESEND_API_KEY set   -> send via Resend (raises on API error).
    - RESEND_API_KEY unset -> log only, treated as success (dev/test).
    - fail-delivery+*      -> raise ValueError to simulate delivery failure.
    """
    # Test-only hook
    if notification.recipient_email.startswith("fail-delivery+"):
        raise ValueError("Notification delivery rejected for test simulation")

    config = get_settings()
    if not config.resend_api_key:
        logger.debug(
            "RESEND_API_KEY not configured — skipping delivery for %s (event=%s)",
            notification.recipient_email,
            notification.event_type,
        )
        return  # treat as success in dev/test

    from_addr = f"{config.email_from_name} <{config.email_from_address}>"
    _send_via_resend(notification, config.resend_api_key, from_addr)
    logger.info(
        "Email delivered via Resend: event=%s to=%s",
        notification.event_type,
        notification.recipient_email,
    )


def process_pending_notifications(db, limit: int = 25) -> dict[str, int]:
    """
    Flush up to `limit` PENDING notifications.
    Called by the scheduler job and the /notifications/process admin endpoint.
    """
    notifications = (
        db.query(models.NotificationOutbox)
        .filter(models.NotificationOutbox.status == models.NotificationStatus.PENDING)
        .order_by(
            models.NotificationOutbox.created_at.asc(),
            models.NotificationOutbox.id.asc(),
        )
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
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Notification delivery failed: id=%s event=%s error=%s",
                notification.id,
                notification.event_type,
                exc,
            )
            notification.status = models.NotificationStatus.FAILED
            notification.failure_reason = str(exc)[:490]
            failed += 1

    if notifications:
        db.commit()

    return {
        "processed": len(notifications),
        "sent": sent,
        "failed": failed,
    }
