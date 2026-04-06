"""
Razorpay payment gateway endpoints.
Supports: UPI, GPay, PhonePe, Debit/Credit cards via Razorpay.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

import models
from database import get_db, settings
from services.inventory_service import confirm_inventory_for_booking
from services.notification_service import (
    queue_booking_confirmation_email,
    queue_payment_failure_email,
    queue_payment_receipt_email,
)

router = APIRouter(prefix="/payments/razorpay", tags=["Razorpay Payments"])

RAZORPAY_UPI_METHODS = {"upi", "gpay", "phonepay", "phonepe", "bhim"}


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
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(status_code=409, detail="Booking already paid")
    if booking.status in (models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED):
        raise HTTPException(status_code=400, detail="Cancelled or expired bookings cannot be paid")
    return booking


def _generate_txn_ref() -> str:
    return "TXN-RZP-" + secrets.token_hex(6).upper()


@router.post("/create-order")
async def create_razorpay_order(
    booking_id: int = Body(...),
    payment_method: str = Body(...),  # upi, gpay, phonepay, card, netbanking
    idempotency_key: Optional[str] = Body(None),
    db: Session = Depends(get_db),
):
    """Create a Razorpay order for the given booking."""
    booking = _get_booking_for_payment(db, booking_id)
    client = _get_razorpay_client()

    # Normalise method
    method = payment_method.lower().replace("phonepay", "phonepe")
    if method not in ("upi", "gpay", "phonepe", "bhim", "card", "netbanking", "wallet", "mock"):
        raise HTTPException(
            status_code=422,
            detail="Invalid payment_method. Use: upi, gpay, phonepe, card, netbanking, wallet",
        )

    # Check for existing pending transaction with same idempotency key
    if idempotency_key:
        existing = db.query(models.Transaction).filter(
            models.Transaction.idempotency_key == idempotency_key
        ).first()
        if existing and existing.razorpay_order_id:
            return {
                "order_id": existing.razorpay_order_id,
                "transaction_ref": existing.transaction_ref,
                "amount_paise": int(existing.amount * 100),
                "currency": existing.currency,
                "key_id": settings.razorpay_key_id,
                "idempotent": True,
            }

    # Amount in paise (Razorpay uses smallest currency unit)
    amount_paise = int(booking.total_amount * 100)

    if method == "mock":
        # Mock order for test environments
        razorpay_order_id = f"order_mock_{secrets.token_hex(8)}"
    else:
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
            raise HTTPException(status_code=502, detail=f"Razorpay order creation failed: {exc}") from exc

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
    db.commit()
    db.refresh(transaction)

    return {
        "order_id": razorpay_order_id,
        "transaction_ref": txn_ref,
        "amount_paise": amount_paise,
        "currency": "INR",
        "key_id": settings.razorpay_key_id,
        "idempotent": False,
    }


@router.post("/verify-payment")
async def verify_razorpay_payment(
    razorpay_order_id: str = Body(...),
    razorpay_payment_id: str = Body(...),
    razorpay_signature: str = Body(...),
    transaction_ref: str = Body(...),
    db: Session = Depends(get_db),
):
    """Verify Razorpay payment signature and confirm booking."""
    transaction = db.query(models.Transaction).filter(
        models.Transaction.transaction_ref == transaction_ref
    ).first()
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if transaction.status == models.TransactionStatus.SUCCESS:
        return {"status": "success", "message": "Payment already confirmed"}

    # Verify HMAC signature
    key_secret = settings.razorpay_key_secret
    if key_secret:
        payload_str = f"{razorpay_order_id}|{razorpay_payment_id}"
        expected_sig = hmac.new(
            key_secret.encode(),
            payload_str.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, razorpay_signature):
            transaction.status = models.TransactionStatus.FAILED
            transaction.failure_reason = "Signature verification failed"
            db.commit()
            raise HTTPException(status_code=400, detail="Payment signature verification failed")

    # Mark transaction success
    transaction.razorpay_payment_id = razorpay_payment_id
    transaction.razorpay_signature = razorpay_signature
    transaction.status = models.TransactionStatus.SUCCESS

    booking = db.query(models.Booking).filter(
        models.Booking.id == transaction.booking_id
    ).first()
    if booking:
        booking.payment_status = models.PaymentStatus.PAID
        booking.status = models.BookingStatus.CONFIRMED
        confirm_inventory_for_booking(db, booking=booking)
        queue_booking_confirmation_email(db, booking, transaction)
        queue_payment_receipt_email(db, booking, transaction)

    db.commit()
    return {
        "status": "success",
        "transaction_ref": transaction_ref,
        "razorpay_payment_id": razorpay_payment_id,
        "booking_status": booking.status.value if booking else None,
    }


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Handle Razorpay webhook events."""
    body = await request.body()

    # Verify webhook signature if secret configured
    webhook_secret = settings.razorpay_webhook_secret
    if webhook_secret and x_razorpay_signature:
        expected = hmac.new(
            webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, x_razorpay_signature):
            raise HTTPException(status_code=400, detail="Webhook signature invalid")

    import json
    try:
        event = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event_type = event.get("event", "")
    payload = event.get("payload", {})

    if event_type == "payment.captured":
        payment = payload.get("payment", {}).get("entity", {})
        razorpay_order_id = payment.get("order_id")
        razorpay_payment_id = payment.get("id")
        notes = payment.get("notes", {})
        _booking_id = notes.get("booking_id")

        if razorpay_order_id:
            transaction = db.query(models.Transaction).filter(
                models.Transaction.razorpay_order_id == razorpay_order_id
            ).first()
            if transaction and transaction.status != models.TransactionStatus.SUCCESS:
                transaction.razorpay_payment_id = razorpay_payment_id
                transaction.status = models.TransactionStatus.SUCCESS
                booking = db.query(models.Booking).filter(
                    models.Booking.id == transaction.booking_id
                ).first()
                if booking and booking.payment_status != models.PaymentStatus.PAID:
                    booking.payment_status = models.PaymentStatus.PAID
                    booking.status = models.BookingStatus.CONFIRMED
                    confirm_inventory_for_booking(db, booking=booking)
                    queue_booking_confirmation_email(db, booking, transaction)
                    queue_payment_receipt_email(db, booking, transaction)
                db.commit()

    elif event_type == "payment.failed":
        payment = payload.get("payment", {}).get("entity", {})
        razorpay_order_id = payment.get("order_id")
        if razorpay_order_id:
            transaction = db.query(models.Transaction).filter(
                models.Transaction.razorpay_order_id == razorpay_order_id
            ).first()
            if transaction and transaction.status == models.TransactionStatus.PENDING:
                transaction.status = models.TransactionStatus.FAILED
                transaction.failure_reason = payment.get("error_description", "Payment failed")
                booking = db.query(models.Booking).filter(
                    models.Booking.id == transaction.booking_id
                ).first()
                if booking:
                    booking.payment_status = models.PaymentStatus.FAILED
                    queue_payment_failure_email(db, booking, transaction, payment.get("error_description", "Payment failed"))
                db.commit()

    return {"status": "ok"}
