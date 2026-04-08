"""
Razorpay payment gateway endpoints.
Supports: UPI, GPay, PhonePe, Cards, Net Banking, Wallets via Razorpay.

Phases covered:
  1. Payment methods: UPI, cards, net banking, wallets
  2. Order creation with idempotency
  3. Signature verification (HMAC SHA-256)
  4. Webhook: payment.captured, payment.failed, refund.processed
  5. Hold expiry edge case: auto-refund if payment succeeds after hold expiry
  6. Refund flow: full + partial via Razorpay API
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

import models
from database import get_db, settings
from routers.auth import get_current_admin
from services.audit_service import write_audit_log
from services.inventory_service import (
    confirm_inventory_for_booking,
)
from services.notification_service import (
    queue_booking_confirmation_email,
    queue_payment_failure_email,
    queue_payment_receipt_email,
    queue_refund_initiated_email,
    queue_refund_success_email,
    queue_admin_alert_email,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments/razorpay", tags=["Razorpay Payments"])

RAZORPAY_VALID_METHODS = {
    "upi", "gpay", "phonepe", "phonepay", "bhim",
    "card", "netbanking", "wallet", "mock",
}
REFUND_SETTLEMENT_DAYS = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_razorpay_client():
    """Return a Razorpay client. Raises 503 if keys not configured."""
    key_id = settings.razorpay_key_id
    key_secret = settings.razorpay_key_secret
    if not key_id or not key_secret:
        raise HTTPException(
            status_code=503,
            detail="Razorpay is not configured on this server",
        )
    try:
        import razorpay  # type: ignore
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="razorpay package not installed") from exc
    return razorpay.Client(auth=(key_id, key_secret))


def _get_booking_for_payment(db: Session, booking_id: int) -> models.Booking:
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(status_code=409, detail="Booking already paid")
    if booking.status in (models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED):
        raise HTTPException(status_code=400, detail="Cancelled or expired bookings cannot be paid")
    return booking


def _generate_txn_ref() -> str:
    return "TXN-RZP-" + secrets.token_hex(6).upper()


def _verify_razorpay_signature(
    order_id: str,
    payment_id: str,
    signature: str,
    key_secret: str,
) -> bool:
    """Verify HMAC SHA-256 signature from Razorpay."""
    payload_str = f"{order_id}|{payment_id}"
    expected_sig = hmac.new(
        key_secret.encode(),
        payload_str.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_sig, signature)


def _verify_webhook_signature(body: bytes, signature: str, webhook_secret: str) -> bool:
    """Verify Razorpay webhook signature."""
    expected = hmac.new(
        webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _is_hold_expired(booking: models.Booking) -> bool:
    """Check if the booking hold has expired."""
    if not booking.hold_expires_at:
        return False
    now = utc_now()
    hold_expires = booking.hold_expires_at
    if hold_expires.tzinfo is None:
        hold_expires = hold_expires.replace(tzinfo=timezone.utc)
    return now > hold_expires


def _write_razorpay_audit(
    db: Session,
    *,
    action: str,
    booking: Optional[models.Booking] = None,
    transaction: Optional[models.Transaction] = None,
    metadata: Optional[dict] = None,
) -> None:
    entity_id = booking.id if booking else (transaction.id if transaction else "unknown")
    entity_type = "booking" if booking else "payment"
    write_audit_log(
        db,
        actor_user_id=None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=metadata or {},
    )


def _apply_paid_state(booking: models.Booking) -> None:
    """Mark booking as paid + confirmed."""
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED


def _find_transaction_by_order_id(
    db: Session, razorpay_order_id: str
) -> Optional[models.Transaction]:
    return (
        db.query(models.Transaction)
        .filter(models.Transaction.razorpay_order_id == razorpay_order_id)
        .first()
    )


def _find_transaction_by_payment_id(
    db: Session, razorpay_payment_id: str
) -> Optional[models.Transaction]:
    return (
        db.query(models.Transaction)
        .filter(models.Transaction.razorpay_payment_id == razorpay_payment_id)
        .first()
    )


# ─── Phase 1-2: Create Order ────────────────────────────────────────────────


@router.post("/create-order")
async def create_razorpay_order(
    booking_id: int = Body(...),
    payment_method: str = Body(...),
    idempotency_key: Optional[str] = Body(None),
    db: Session = Depends(get_db),
):
    """Create a Razorpay order for the given booking.

    Supports: upi, gpay, phonepe, card, netbanking, wallet, mock.
    """
    booking = _get_booking_for_payment(db, booking_id)

    # Normalise method
    method = payment_method.lower().replace("phonepay", "phonepe")
    if method not in RAZORPAY_VALID_METHODS:
        raise HTTPException(
            status_code=422,
            detail="Invalid payment_method. Use: upi, gpay, phonepe, card, netbanking, wallet",
        )

    # Idempotency: return existing transaction if same key was used
    if idempotency_key:
        existing = (
            db.query(models.Transaction)
            .filter(models.Transaction.idempotency_key == idempotency_key)
            .first()
        )
        if existing and existing.razorpay_order_id:
            return {
                "order_id": existing.razorpay_order_id,
                "transaction_ref": existing.transaction_ref,
                "amount_paise": int(existing.amount * 100),
                "currency": existing.currency,
                "key_id": settings.razorpay_key_id,
                "idempotent": True,
            }

    # Amount in paise (smallest INR unit)
    amount_paise = int(booking.total_amount * 100)

    if method == "mock":
        razorpay_order_id = f"order_mock_{secrets.token_hex(8)}"
    else:
        client = _get_razorpay_client()
        try:
            order = client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "receipt": f"bk_{booking.id}_{secrets.token_hex(4)}",
                "notes": {
                    "booking_id": str(booking.id),
                    "booking_ref": booking.booking_ref,
                },
            })
            razorpay_order_id = order["id"]
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Razorpay order creation failed: {exc}",
            ) from exc

    txn_ref = _generate_txn_ref()
    transaction = models.Transaction(
        booking_id=booking.id,
        transaction_ref=txn_ref,
        razorpay_order_id=razorpay_order_id,
        gateway="razorpay",
        idempotency_key=idempotency_key or txn_ref,
        amount=booking.total_amount,
        currency="INR",
        payment_method=method,
        status=models.TransactionStatus.PENDING,
    )
    db.add(transaction)

    # Set booking to processing state
    if booking.status == models.BookingStatus.PENDING:
        booking.status = models.BookingStatus.PROCESSING
    if booking.payment_status == models.PaymentStatus.PENDING:
        booking.payment_status = models.PaymentStatus.PROCESSING

    db.commit()
    db.refresh(transaction)

    _write_razorpay_audit(
        db,
        action="razorpay.order.created",
        booking=booking,
        transaction=transaction,
        metadata={
            "razorpay_order_id": razorpay_order_id,
            "payment_method": method,
            "amount_paise": amount_paise,
        },
    )

    return {
        "order_id": razorpay_order_id,
        "transaction_ref": txn_ref,
        "amount_paise": amount_paise,
        "currency": "INR",
        "key_id": settings.razorpay_key_id,
        "idempotent": False,
    }


# ─── Phase 3: Verify Payment (Frontend Callback) ────────────────────────────


@router.post("/verify-payment")
async def verify_razorpay_payment(
    razorpay_order_id: str = Body(...),
    razorpay_payment_id: str = Body(...),
    razorpay_signature: str = Body(...),
    transaction_ref: str = Body(...),
    db: Session = Depends(get_db),
):
    """Verify Razorpay payment signature and confirm booking.

    This is called by the frontend after Razorpay checkout success callback.
    The webhook is the source of truth — this provides immediate feedback.
    """
    transaction = (
        db.query(models.Transaction)
        .filter(models.Transaction.transaction_ref == transaction_ref)
        .first()
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Idempotent: if already success, return immediately
    if transaction.status == models.TransactionStatus.SUCCESS:
        return {
            "status": "success",
            "message": "Payment already confirmed",
            "transaction_ref": transaction_ref,
            "razorpay_payment_id": transaction.razorpay_payment_id,
        }

    # Step 1: Verify HMAC signature
    key_secret = settings.razorpay_key_secret
    if not key_secret:
        raise HTTPException(
            status_code=503,
            detail="Razorpay key secret is not configured. Cannot verify payment.",
        )

    if not _verify_razorpay_signature(
        razorpay_order_id, razorpay_payment_id, razorpay_signature, key_secret
    ):
        transaction.status = models.TransactionStatus.FAILED
        transaction.failure_reason = "Signature verification failed"
        db.commit()
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    # Step 2: Server-to-server verification with Razorpay API
    client = _get_razorpay_client()
    try:
        payment = client.payment.fetch(razorpay_payment_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to verify payment with Razorpay: {exc}",
        ) from exc

    payment_status = payment.get("status", "")
    if payment_status == "authorized":
        try:
            client.payment.capture(razorpay_payment_id, payment.get("amount", 0))
        except Exception as exc:
            transaction.status = models.TransactionStatus.FAILED
            transaction.failure_reason = f"Payment capture failed: {exc}"
            db.commit()
            raise HTTPException(
                status_code=400,
                detail=f"Payment capture failed: {exc}",
            ) from exc
    elif payment_status != "captured":
        transaction.status = models.TransactionStatus.FAILED
        transaction.failure_reason = f"Payment not captured. Status: {payment_status}"
        db.commit()
        raise HTTPException(
            status_code=400,
            detail=f"Payment not completed. Razorpay status: {payment_status}",
        )

    # Verify order ID and amount match
    if payment.get("order_id") != razorpay_order_id:
        transaction.status = models.TransactionStatus.FAILED
        transaction.failure_reason = "Order ID mismatch"
        db.commit()
        raise HTTPException(status_code=400, detail="Payment order ID mismatch")

    expected_amount = int(transaction.amount * 100)
    if payment.get("amount") != expected_amount:
        transaction.status = models.TransactionStatus.FAILED
        transaction.failure_reason = f"Amount mismatch: expected {expected_amount}, got {payment.get('amount')}"
        db.commit()
        raise HTTPException(status_code=400, detail="Payment amount mismatch")

    # Step 3: All checks passed — confirm payment
    transaction.razorpay_payment_id = razorpay_payment_id
    transaction.razorpay_signature = razorpay_signature
    transaction.status = models.TransactionStatus.SUCCESS
    transaction.failure_reason = None

    # Extract card details if available
    card_info = payment.get("card", {})
    if card_info:
        transaction.card_last4 = card_info.get("last4")
        transaction.card_brand = card_info.get("network")

    # Update payment method from Razorpay's actual method
    actual_method = payment.get("method", transaction.payment_method)
    if actual_method:
        transaction.payment_method = actual_method

    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == transaction.booking_id)
        .first()
    )

    if not booking:
        db.commit()
        return {
            "status": "success",
            "transaction_ref": transaction_ref,
            "razorpay_payment_id": razorpay_payment_id,
            "booking_status": None,
            "warning": "Booking not found",
        }

    # ── Phase 6: Hold expiry edge case ──────────────────────────────────
    if _is_hold_expired(booking) and booking.status != models.BookingStatus.CONFIRMED:
        # Payment arrived after hold expired — DO NOT confirm, auto-refund
        transaction.status = models.TransactionStatus.SUCCESS
        booking.payment_status = models.PaymentStatus.PAID
        # Trigger auto-refund
        booking.refund_status = models.RefundStatus.REFUND_REQUESTED
        booking.refund_amount = booking.total_amount
        booking.refund_requested_at = utc_now()
        booking.refund_failed_reason = "Auto-refund: payment received after hold expiry"
        booking.status = models.BookingStatus.EXPIRED

        _write_razorpay_audit(
            db,
            action="razorpay.payment.expired_hold_autorefund",
            booking=booking,
            transaction=transaction,
            metadata={
                "razorpay_payment_id": razorpay_payment_id,
                "hold_expires_at": str(booking.hold_expires_at),
            },
        )

        # Queue admin alert for auto-refund
        queue_admin_alert_email(
            db,
            recipient_email=settings.seed_admin_email or "ops@stayvora.co.in",
            subject=f"Auto-refund triggered — {booking.booking_ref}",
            body=(
                f"Payment received after hold expiry for booking {booking.booking_ref}.\n"
                f"Razorpay payment: {razorpay_payment_id}\n"
                f"Amount: INR {booking.total_amount:.2f}\n\n"
                "An automatic refund has been requested. Please process via admin panel."
            ),
            booking_id=booking.id,
            transaction_id=transaction.id,
            event_type="hold_expired_autorefund",
        )

        db.commit()
        return {
            "status": "expired",
            "message": "Hold expired. Payment will be refunded automatically.",
            "transaction_ref": transaction_ref,
            "razorpay_payment_id": razorpay_payment_id,
            "booking_status": "expired",
            "refund_requested": True,
        }

    # Normal success path — confirm booking
    _apply_paid_state(booking)
    confirm_inventory_for_booking(db, booking=booking)
    db.flush()
    queue_booking_confirmation_email(db, booking, transaction)
    queue_payment_receipt_email(db, booking, transaction)

    _write_razorpay_audit(
        db,
        action="razorpay.payment.verified",
        booking=booking,
        transaction=transaction,
        metadata={
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_order_id": razorpay_order_id,
            "payment_method": actual_method,
        },
    )

    db.commit()
    return {
        "status": "success",
        "transaction_ref": transaction_ref,
        "razorpay_payment_id": razorpay_payment_id,
        "booking_status": booking.status.value if booking else None,
    }


# ─── Phase 4: Record Payment Failure ────────────────────────────────────────


@router.post("/payment-failure")
async def record_razorpay_failure(
    razorpay_order_id: str = Body(...),
    error_code: Optional[str] = Body(None),
    error_description: Optional[str] = Body(None),
    error_reason: Optional[str] = Body(None),
    db: Session = Depends(get_db),
):
    """Record a Razorpay payment failure (popup close, decline, timeout)."""
    transaction = _find_transaction_by_order_id(db, razorpay_order_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if transaction.status == models.TransactionStatus.SUCCESS:
        return {"status": "already_paid", "message": "Payment was already successful"}

    reason_parts = [
        p for p in [error_code, error_description, error_reason] if p
    ]
    reason = " | ".join(reason_parts) if reason_parts else "Payment failed or cancelled by user"

    transaction.status = models.TransactionStatus.FAILED
    transaction.failure_reason = reason[:490]

    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == transaction.booking_id)
        .first()
    )
    if booking and booking.payment_status != models.PaymentStatus.PAID:
        booking.payment_status = models.PaymentStatus.FAILED

        # Queue failure notification
        queue_payment_failure_email(db, booking, transaction, reason[:200])

    _write_razorpay_audit(
        db,
        action="razorpay.payment.failed",
        booking=booking,
        transaction=transaction,
        metadata={
            "error_code": error_code,
            "error_description": error_description,
            "error_reason": error_reason,
        },
    )

    db.commit()
    return {
        "status": "recorded",
        "transaction_ref": transaction.transaction_ref,
        "failure_reason": reason[:200],
    }


# ─── Phase 5: Webhook Handler (Source of Truth) ─────────────────────────────


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Handle Razorpay webhook events.

    Events handled:
      - payment.captured → confirm booking (or auto-refund if hold expired)
      - payment.failed   → mark transaction failed
      - refund.processed → update refund status

    All handlers are IDEMPOTENT — safe to receive duplicates.
    """
    body = await request.body()

    # Verify webhook signature if secret configured
    webhook_secret = settings.razorpay_webhook_secret
    if webhook_secret and x_razorpay_signature:
        if not _verify_webhook_signature(body, x_razorpay_signature, webhook_secret):
            raise HTTPException(status_code=400, detail="Webhook signature invalid")

    try:
        event = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event_type = event.get("event", "")
    payload = event.get("payload", {})

    logger.info("Razorpay webhook received: event=%s", event_type)

    if event_type == "payment.captured":
        return _handle_payment_captured(db, payload)
    if event_type == "payment.failed":
        return _handle_payment_failed(db, payload)
    if event_type == "refund.processed":
        return _handle_refund_processed(db, payload)

    logger.debug("Unhandled Razorpay webhook event: %s", event_type)
    return {"status": "ignored", "event": event_type}


def _handle_payment_captured(db: Session, payload: dict) -> dict:
    """Handle payment.captured webhook — source of truth for payment confirmation."""
    payment_entity = payload.get("payment", {}).get("entity", {})
    razorpay_payment_id = payment_entity.get("id", "")
    razorpay_order_id = payment_entity.get("order_id", "")
    amount_paise = payment_entity.get("amount", 0)
    payment_method = payment_entity.get("method", "")

    if not razorpay_order_id:
        return {"status": "skipped", "reason": "no order_id in payment"}

    # Find transaction by order ID
    transaction = _find_transaction_by_order_id(db, razorpay_order_id)
    if not transaction:
        logger.warning(
            "Webhook payment.captured: no transaction for order %s", razorpay_order_id
        )
        return {"status": "skipped", "reason": "transaction not found"}

    # Idempotent: already processed
    if transaction.status == models.TransactionStatus.SUCCESS:
        return {"status": "already_processed", "transaction_ref": transaction.transaction_ref}

    # Update transaction
    transaction.razorpay_payment_id = razorpay_payment_id
    transaction.status = models.TransactionStatus.SUCCESS
    transaction.failure_reason = None
    if payment_method:
        transaction.payment_method = payment_method

    # Extract card details
    card_info = payment_entity.get("card", {})
    if card_info:
        transaction.card_last4 = card_info.get("last4")
        transaction.card_brand = card_info.get("network")

    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == transaction.booking_id)
        .first()
    )

    if not booking:
        db.commit()
        return {"status": "processed", "warning": "booking not found"}

    # ── Phase 6: Hold expiry check ──────────────────────────────────────
    if _is_hold_expired(booking) and booking.status != models.BookingStatus.CONFIRMED:
        booking.payment_status = models.PaymentStatus.PAID
        booking.refund_status = models.RefundStatus.REFUND_REQUESTED
        booking.refund_amount = booking.total_amount
        booking.refund_requested_at = utc_now()
        booking.refund_failed_reason = "Auto-refund: webhook payment.captured after hold expiry"
        booking.status = models.BookingStatus.EXPIRED

        queue_admin_alert_email(
            db,
            recipient_email=settings.seed_admin_email or "ops@stayvora.co.in",
            subject=f"[Webhook] Auto-refund triggered — {booking.booking_ref}",
            body=(
                f"Razorpay payment.captured webhook received after hold expiry.\n"
                f"Booking: {booking.booking_ref}\n"
                f"Payment ID: {razorpay_payment_id}\n"
                f"Amount: INR {amount_paise / 100:.2f}\n"
            ),
            booking_id=booking.id,
            transaction_id=transaction.id,
            event_type="webhook_hold_expired_autorefund",
        )

        _write_razorpay_audit(
            db,
            action="razorpay.webhook.payment_captured.expired_hold",
            booking=booking,
            transaction=transaction,
            metadata={
                "razorpay_payment_id": razorpay_payment_id,
                "hold_expires_at": str(booking.hold_expires_at),
            },
        )

        db.commit()
        return {
            "status": "expired_hold_autorefund",
            "booking_ref": booking.booking_ref,
        }

    # Normal path — confirm booking
    if booking.payment_status != models.PaymentStatus.PAID:
        _apply_paid_state(booking)
        confirm_inventory_for_booking(db, booking=booking)
        db.flush()
        queue_booking_confirmation_email(db, booking, transaction)
        queue_payment_receipt_email(db, booking, transaction)

    _write_razorpay_audit(
        db,
        action="razorpay.webhook.payment_captured",
        booking=booking,
        transaction=transaction,
        metadata={
            "razorpay_payment_id": razorpay_payment_id,
            "amount_paise": amount_paise,
            "method": payment_method,
        },
    )

    db.commit()
    return {
        "status": "confirmed",
        "booking_ref": booking.booking_ref,
        "transaction_ref": transaction.transaction_ref,
    }


def _handle_payment_failed(db: Session, payload: dict) -> dict:
    """Handle payment.failed webhook."""
    payment_entity = payload.get("payment", {}).get("entity", {})
    razorpay_order_id = payment_entity.get("order_id", "")
    error_code = payment_entity.get("error_code", "")
    error_description = payment_entity.get("error_description", "")

    if not razorpay_order_id:
        return {"status": "skipped", "reason": "no order_id"}

    transaction = _find_transaction_by_order_id(db, razorpay_order_id)
    if not transaction:
        return {"status": "skipped", "reason": "transaction not found"}

    # Idempotent: don't overwrite success
    if transaction.status == models.TransactionStatus.SUCCESS:
        return {"status": "ignored", "reason": "transaction already succeeded"}

    reason = f"{error_code}: {error_description}" if error_code else "Payment failed (webhook)"
    transaction.status = models.TransactionStatus.FAILED
    transaction.failure_reason = reason[:490]

    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == transaction.booking_id)
        .first()
    )
    if booking and booking.payment_status != models.PaymentStatus.PAID:
        booking.payment_status = models.PaymentStatus.FAILED

    _write_razorpay_audit(
        db,
        action="razorpay.webhook.payment_failed",
        booking=booking,
        transaction=transaction,
        metadata={
            "error_code": error_code,
            "error_description": error_description,
        },
    )

    db.commit()
    return {"status": "recorded", "transaction_ref": transaction.transaction_ref}


def _handle_refund_processed(db: Session, payload: dict) -> dict:
    """Handle refund.processed webhook — update refund status on booking."""
    refund_entity = payload.get("refund", {}).get("entity", {})
    razorpay_payment_id = refund_entity.get("payment_id", "")
    refund_id = refund_entity.get("id", "")
    refund_status = refund_entity.get("status", "")
    refund_amount_paise = refund_entity.get("amount", 0)

    if not razorpay_payment_id:
        return {"status": "skipped", "reason": "no payment_id in refund"}

    transaction = _find_transaction_by_payment_id(db, razorpay_payment_id)
    if not transaction:
        logger.warning(
            "Webhook refund.processed: no transaction for payment %s", razorpay_payment_id
        )
        return {"status": "skipped", "reason": "transaction not found"}

    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == transaction.booking_id)
        .first()
    )
    if not booking:
        return {"status": "skipped", "reason": "booking not found"}

    # Idempotent: don't re-process completed refund
    if booking.refund_status == models.RefundStatus.REFUND_SUCCESS:
        return {"status": "already_processed", "booking_ref": booking.booking_ref}

    refund_amount = refund_amount_paise / 100.0

    if refund_status == "processed":
        booking.refund_status = models.RefundStatus.REFUND_SUCCESS
        booking.refund_completed_at = utc_now()
        booking.refund_amount = refund_amount
        booking.refund_gateway_reference = refund_id
        booking.refund_failed_reason = None
        booking.payment_status = models.PaymentStatus.REFUNDED
        booking.status = models.BookingStatus.CANCELLED
        transaction.status = models.TransactionStatus.REFUNDED

        queue_refund_success_email(db, booking)
    elif refund_status == "failed":
        booking.refund_status = models.RefundStatus.REFUND_FAILED
        booking.refund_failed_reason = f"Razorpay refund failed: {refund_id}"
        booking.refund_gateway_reference = refund_id

    _write_razorpay_audit(
        db,
        action=f"razorpay.webhook.refund_{refund_status}",
        booking=booking,
        transaction=transaction,
        metadata={
            "refund_id": refund_id,
            "refund_status": refund_status,
            "refund_amount": refund_amount,
        },
    )

    db.commit()
    return {
        "status": "processed",
        "booking_ref": booking.booking_ref,
        "refund_status": refund_status,
    }


# ─── Phase 7: Refund via Razorpay API ───────────────────────────────────────


@router.post("/refund")
async def initiate_razorpay_refund(
    booking_id: int = Body(...),
    amount: Optional[float] = Body(None),
    reason: str = Body("Refund requested"),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    """Initiate a full or partial refund via Razorpay API.

    - If `amount` is None → full refund
    - If `amount` < total → partial refund
    """
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.payment_status not in (
        models.PaymentStatus.PAID,
        models.PaymentStatus.REFUNDED,
    ):
        raise HTTPException(status_code=400, detail="Only paid bookings can be refunded")

    # Find the successful Razorpay transaction
    transaction = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking.id,
            models.Transaction.gateway == "razorpay",
            models.Transaction.status.in_([
                models.TransactionStatus.SUCCESS,
                models.TransactionStatus.REFUNDED,
            ]),
        )
        .order_by(models.Transaction.id.desc())
        .first()
    )
    if not transaction or not transaction.razorpay_payment_id:
        raise HTTPException(
            status_code=404,
            detail="No Razorpay payment found for this booking",
        )

    refund_amount = amount if amount is not None else booking.total_amount
    if refund_amount <= 0:
        raise HTTPException(status_code=422, detail="Refund amount must be positive")
    if refund_amount > booking.total_amount:
        raise HTTPException(status_code=422, detail="Refund amount exceeds booking total")

    refund_amount_paise = int(refund_amount * 100)

    # Call Razorpay refund API
    client = _get_razorpay_client()
    try:
        refund = client.payment.refund(
            transaction.razorpay_payment_id,
            {
                "amount": refund_amount_paise,
                "notes": {
                    "booking_id": str(booking.id),
                    "booking_ref": booking.booking_ref,
                    "reason": reason[:200],
                },
            },
        )
        refund_id = refund.get("id", "")
    except Exception as exc:
        # Mark as failed but don't crash
        booking.refund_status = models.RefundStatus.REFUND_FAILED
        booking.refund_failed_reason = f"Razorpay API error: {exc}"
        booking.refund_amount = refund_amount
        booking.refund_requested_at = utc_now()
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Razorpay refund API failed: {exc}",
        ) from exc

    # Update booking refund state
    now = utc_now()
    booking.refund_status = models.RefundStatus.REFUND_INITIATED
    booking.refund_amount = refund_amount
    booking.refund_requested_at = booking.refund_requested_at or now
    booking.refund_initiated_at = now
    booking.refund_expected_settlement_at = now + timedelta(days=REFUND_SETTLEMENT_DAYS)
    booking.refund_gateway_reference = refund_id
    booking.refund_failed_reason = reason
    booking.status = models.BookingStatus.CANCELLED

    queue_refund_initiated_email(db, booking)

    _write_razorpay_audit(
        db,
        action="razorpay.refund.initiated",
        booking=booking,
        transaction=transaction,
        metadata={
            "refund_id": refund_id,
            "refund_amount": refund_amount,
            "is_partial": refund_amount < booking.total_amount,
        },
    )

    db.commit()
    return {
        "status": "refund_initiated",
        "booking_ref": booking.booking_ref,
        "refund_id": refund_id,
        "refund_amount": refund_amount,
        "is_partial": refund_amount < booking.total_amount,
        "expected_settlement": booking.refund_expected_settlement_at.isoformat()
        if booking.refund_expected_settlement_at
        else None,
    }


# ─── Refund Timeline ────────────────────────────────────────────────────────


@router.get("/refund-timeline/{booking_id}")
async def get_razorpay_refund_timeline(
    booking_id: int,
    db: Session = Depends(get_db),
):
    """Get refund timeline for a booking."""
    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if not booking.refund_status:
        raise HTTPException(status_code=404, detail="No refund found for this booking")

    return {
        "booking_id": booking.id,
        "booking_ref": booking.booking_ref,
        "refund_status": booking.refund_status.value if booking.refund_status else None,
        "refund_amount": booking.refund_amount,
        "requested_at": booking.refund_requested_at.isoformat() if booking.refund_requested_at else None,
        "initiated_at": booking.refund_initiated_at.isoformat() if booking.refund_initiated_at else None,
        "expected_settlement_at": booking.refund_expected_settlement_at.isoformat()
        if booking.refund_expected_settlement_at
        else None,
        "completed_at": booking.refund_completed_at.isoformat() if booking.refund_completed_at else None,
        "failed_reason": booking.refund_failed_reason,
        "gateway_reference": booking.refund_gateway_reference,
    }


# ─── Payment Status (for polling) ───────────────────────────────────────────


@router.get("/status/{booking_id}")
async def get_razorpay_payment_status(
    booking_id: int,
    db: Session = Depends(get_db),
):
    """Get payment status for frontend polling.

    Returns the latest transaction status and booking state
    so the frontend can detect webhook-confirmed payments.
    """
    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    latest_txn = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking_id,
            models.Transaction.gateway == "razorpay",
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )

    # Count failed attempts for retry policy
    failed_count = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking_id,
            models.Transaction.gateway == "razorpay",
            models.Transaction.status == models.TransactionStatus.FAILED,
        )
        .count()
    )

    return {
        "booking_id": booking.id,
        "booking_ref": booking.booking_ref,
        "booking_status": booking.status.value,
        "payment_status": booking.payment_status.value,
        "hold_expires_at": booking.hold_expires_at.isoformat() if booking.hold_expires_at else None,
        "hold_expired": _is_hold_expired(booking),
        "latest_transaction": {
            "transaction_ref": latest_txn.transaction_ref,
            "status": latest_txn.status.value,
            "razorpay_order_id": latest_txn.razorpay_order_id,
            "razorpay_payment_id": latest_txn.razorpay_payment_id,
            "payment_method": latest_txn.payment_method,
            "failure_reason": latest_txn.failure_reason,
        }
        if latest_txn
        else None,
        "failed_payment_count": failed_count,
        "retry_after_seconds": 180 if failed_count >= 5 else 0,
        "refund_status": booking.refund_status.value if booking.refund_status else None,
    }
