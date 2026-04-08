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

import base64
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
    attachment_pdf: Optional[bytes] = None,
    attachment_filename: Optional[str] = None,
) -> models.NotificationOutbox:
    notification = models.NotificationOutbox(
        booking_id=booking_id,
        transaction_id=transaction_id,
        event_type=event_type,
        recipient_email=recipient_email,
        subject=subject,
        body=body,
        status=models.NotificationStatus.PENDING,
        attachment_pdf=attachment_pdf,
        attachment_filename=attachment_filename,
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
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Hold Reserved!</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">Your reservation hold is active and ready for payment.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">HOLD DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Ref:</strong> {booking.booking_ref}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Expires:</strong> {booking.hold_expires_at} UTC</p>'
            f'</div>'
            f'<p style="color: #4a5568; line-height: 1.7;">Complete payment before the hold expires to confirm your stay.</p>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
        ),
    )


def queue_booking_confirmation_email(
    db, booking: models.Booking, transaction: models.Transaction
) -> models.NotificationOutbox:
    # Generate invoice PDF to attach
    from services.document_service import build_invoice_pdf, invoice_number_for_booking

    try:
        pdf_bytes = build_invoice_pdf(booking)
        filename = f"{invoice_number_for_booking(booking)}.pdf"
    except Exception:  # noqa: BLE001
        logger.warning("Failed to generate invoice PDF for booking %s", booking.booking_ref)
        pdf_bytes = None
        filename = None

    return enqueue_notification(
        db,
        event_type="booking_confirmed",
        recipient_email=booking.email,
        booking_id=booking.id,
        transaction_id=transaction.id,
        subject=f"Booking Confirmed — {booking.booking_ref} | Your Invoice is Attached",
        body=(
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Booking Confirmed!</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">Great news — your booking is confirmed.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">BOOKING DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Ref:</strong> {booking.booking_ref}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Hotel:</strong> {booking.room.hotel_name if booking.room else "Stayvora Hotel"}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Room:</strong> {booking.room.room_type_name if booking.room and booking.room.room_type_name else "Room"}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Check-in:</strong> {booking.check_in}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Check-out:</strong> {booking.check_out}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Guests:</strong> {booking.guests}</p>'
            f'<p style="margin: 8px 0 0; color: #0f2033; font-size: 18px;"><strong>Total: INR {booking.total_amount:.2f}</strong></p>'
            f'</div>'
            f'<p style="color: #4a5568; line-height: 1.7;">Your tax invoice is attached. You can also download it from the Stayvora app.</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">We hope you have a wonderful stay!</p>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
        ),
        attachment_pdf=pdf_bytes,
        attachment_filename=filename,
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
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Payment Received</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">We have received your payment.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">PAYMENT DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Amount:</strong> INR {transaction.amount:.2f}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Booking Ref:</strong> {booking.booking_ref}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Transaction:</strong> {transaction.transaction_ref}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Method:</strong> {transaction.payment_method or "Card"}</p>'
            f'</div>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
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
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Payment Failed</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">Your payment for booking {booking.booking_ref} could not be processed.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">FAILURE DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Reason:</strong> {reason}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Booking Ref:</strong> {booking.booking_ref}</p>'
            f'</div>'
            f'<p style="color: #4a5568; line-height: 1.7;">Your reservation hold remains active. Please retry payment to confirm your booking.</p>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
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
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Booking Cancelled</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">Your booking has been cancelled.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">BOOKING DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Ref:</strong> {booking.booking_ref}</p>'
            f'</div>'
            f'<p style="color: #4a5568; line-height: 1.7;">If you did not request this cancellation, please contact support immediately at support@stayvora.co.in.</p>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
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
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Refund Initiated</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">Your refund has been initiated.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">REFUND DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Amount:</strong> INR {booking.refund_amount:.2f}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Booking Ref:</strong> {booking.booking_ref}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Expected:</strong> {settlement}</p>'
            f'</div>'
            f'<p style="color: #4a5568; line-height: 1.7;">You will receive another email once the refund has been completed.</p>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
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
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Refund Processed</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">Your refund has been processed successfully.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">REFUND DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Amount:</strong> INR {booking.refund_amount:.2f}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Booking Ref:</strong> {booking.booking_ref}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Reference:</strong> {booking.refund_gateway_reference or "N/A"}</p>'
            f'</div>'
            f'<p style="color: #4a5568; line-height: 1.7;">The refund will appear in your account within 2-5 business days.</p>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
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
            f'<div style="font-family: Inter, -apple-system, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">'
            f'<div style="background: #0f2033; padding: 32px; text-align: center;">'
            f'<h1 style="color: #d6b86b; font-family: Playfair Display, Georgia, serif; font-size: 28px; margin: 0;">Stayvora</h1>'
            f'<p style="color: rgba(255,255,255,0.6); font-size: 13px; margin: 8px 0 0;">Premium Hotel Bookings</p>'
            f'</div>'
            f'<div style="padding: 40px 32px;">'
            f'<h2 style="color: #0f2033; font-size: 22px; margin: 0 0 16px;">Refund Issue</h2>'
            f'<p style="color: #4a5568; line-height: 1.7;">Hi {booking.user_name},</p>'
            f'<p style="color: #4a5568; line-height: 1.7;">We encountered an issue processing your refund.</p>'
            f'<div style="background: #f7f5ef; border-radius: 12px; padding: 24px; margin: 24px 0; border-left: 4px solid #d6b86b;">'
            f'<p style="margin: 0 0 8px; color: #718096; font-size: 13px;">ISSUE DETAILS</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Booking Ref:</strong> {booking.booking_ref}</p>'
            f'<p style="margin: 4px 0; color: #2d3748;"><strong>Reason:</strong> {booking.refund_failed_reason or "Processing error"}</p>'
            f'</div>'
            f'<p style="color: #4a5568; line-height: 1.7;">Our support team has been notified and will contact you within 24 hours to resolve this issue.</p>'
            f'</div>'
            f'<div style="background: #0f2033; padding: 24px 32px; text-align: center;">'
            f'<p style="color: rgba(255,255,255,0.5); font-size: 12px; margin: 0;">Stayvora | support@stayvora.co.in | www.stayvora.co.in</p>'
            f'</div>'
            f'</div>'
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
    params: dict = {
        "from": from_addr,
        "to": [notification.recipient_email],
        "subject": notification.subject,
        "html": notification.body,
        "text": notification.body,
    }

    # Attach PDF if present
    if notification.attachment_pdf and notification.attachment_filename:
        b64_content = base64.b64encode(notification.attachment_pdf).decode("ascii")
        params["attachments"] = [
            {
                "filename": notification.attachment_filename,
                "content": b64_content,
                "content_type": "application/pdf",
            }
        ]

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
    if config.app_env.lower() != "production":
        logger.debug(
            "Non-production runtime â€” skipping live delivery for %s (event=%s)",
            notification.recipient_email,
            notification.event_type,
        )
        return
    if not config.resend_api_key:
        logger.debug(
            "RESEND_API_KEY not configured — skipping delivery for %s (event=%s)",
            notification.recipient_email,
            notification.event_type,
        )
        return  # treat as success when production mail is not configured

    from_addr = f"{config.email_from_name} <{config.email_from_address}>"
    try:
        _send_via_resend(notification, config.resend_api_key, from_addr)
    except RuntimeError as exc:
        if "resend package is not installed" in str(exc):
            logger.warning(
                "resend package not installed — treating as dev/test success for %s",
                notification.recipient_email,
            )
            return
        raise
    logger.info(
        "Email delivered via Resend: event=%s to=%s attachment=%s",
        notification.event_type,
        notification.recipient_email,
        notification.attachment_filename or "none",
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

    db.commit()
    return {"sent": sent, "failed": failed, "total": sent + failed}
