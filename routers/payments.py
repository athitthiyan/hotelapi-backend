import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db, settings
from routers.auth import get_current_admin
from routers.bookings import expire_stale_booking_hold, release_expired_holds
from services.audit_service import write_audit_log
from services.inventory_service import (
    confirm_inventory_for_booking,
    is_booking_inventory_locked,
    lock_inventory_for_booking,
    release_inventory_for_booking,
)
from services.notification_service import (
    queue_admin_alert_email,
    queue_booking_cancellation_email,
    queue_booking_confirmation_email,
    queue_payment_failure_email,
    queue_payment_receipt_email,
    queue_refund_failure_email,
    queue_refund_initiated_email,
    queue_refund_success_email,
)
from services.payment_state_service import (
    attach_booking_lifecycle_state,
    attach_transaction_lifecycle_state,
    reconcile_gateway_payment_state,
)
from services.rate_limit_service import enforce_rate_limit

router = APIRouter(prefix="/payments", tags=["Payments"])

RECONCILIATION_TIMEOUT_MINUTES = 5
FAILED_PAYMENT_BLOCK_WINDOW_MINUTES = 30
FAILED_PAYMENT_BLOCK_THRESHOLD = 5
FAILED_PAYMENT_BLOCK_SECONDS = 5 * 60
FAILED_PAYMENT_ESCALATED_BLOCK_THRESHOLD = 10
FAILED_PAYMENT_ESCALATED_BLOCK_SECONDS = 15 * 60
REFUND_SETTLEMENT_DAYS = 5


def ops_alert_recipient() -> str:
    return settings.seed_admin_email or "ops@stayvora.co.in"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_txn_ref() -> str:
    return "TXN-" + str(uuid.uuid4()).upper()[:12]


def write_payment_audit(
    db: Session,
    *,
    action: str,
    booking: Optional[models.Booking] = None,
    transaction: Optional[models.Transaction] = None,
    actor_user_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    entity_id = booking.id if booking else (transaction.id if transaction else "unknown")
    entity_type = "booking" if booking else "payment"
    write_audit_log(
        db,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=metadata
        or {
            "booking_id": booking.id if booking else transaction.booking_id if transaction else None,
            "transaction_ref": transaction.transaction_ref if transaction else None,
        },
    )


def queue_payment_ops_alert(
    db: Session,
    *,
    subject: str,
    body: str,
    booking: Optional[models.Booking] = None,
    transaction: Optional[models.Transaction] = None,
    event_type: str = "payment_ops_alert",
) -> None:
    queue_admin_alert_email(
        db,
        recipient_email=ops_alert_recipient(),
        subject=subject,
        body=body,
        booking_id=booking.id if booking else None,
        transaction_id=transaction.id if transaction else None,
        event_type=event_type,
    )


def build_refund_timeline(booking: models.Booking) -> schemas.RefundTimelineResponse:
    if not booking.refund_status:
        raise HTTPException(status_code=404, detail="Refund timeline not found")
    return schemas.RefundTimelineResponse(
        booking_id=booking.id,
        booking_ref=booking.booking_ref,
        refund_status=booking.refund_status,
        refund_amount=booking.refund_amount,
        requested_at=booking.refund_requested_at,
        initiated_at=booking.refund_initiated_at,
        expected_settlement_at=booking.refund_expected_settlement_at,
        completed_at=booking.refund_completed_at,
        failed_reason=booking.refund_failed_reason,
        gateway_reference=booking.refund_gateway_reference,
    )


def get_refundable_booking_or_404(db: Session, booking_id: int) -> tuple[models.Booking, models.Transaction]:
    booking = get_booking_or_404(db, booking_id)
    if booking.payment_status not in [models.PaymentStatus.PAID, models.PaymentStatus.REFUNDED]:
        raise HTTPException(status_code=400, detail="Only paid bookings can be refunded")

    transaction = get_success_transaction_for_booking(db, booking.id)
    if not transaction:
        transaction = (
            db.query(models.Transaction)
            .filter(
                models.Transaction.booking_id == booking.id,
                models.Transaction.status == models.TransactionStatus.REFUNDED,
            )
            .order_by(models.Transaction.id.desc())
            .first()
        )
    if not transaction:
        raise HTTPException(status_code=404, detail="Successful transaction not found")
    return booking, transaction


def ensure_refund_status(
    booking: models.Booking,
    allowed: set[models.RefundStatus | None],
    message: str,
) -> None:
    if booking.refund_status not in allowed:
        raise HTTPException(status_code=409, detail=message)


def set_refund_requested(
    booking: models.Booking,
    *,
    reason: str,
    amount: Optional[float] = None,
) -> None:
    now = utc_now()
    booking.refund_status = models.RefundStatus.REFUND_REQUESTED
    booking.refund_amount = amount if amount is not None else booking.total_amount
    booking.refund_requested_at = now
    booking.refund_failed_reason = reason
    booking.refund_completed_at = None
    booking.refund_initiated_at = None
    booking.refund_expected_settlement_at = None
    booking.refund_gateway_reference = None


def set_refund_initiated(
    booking: models.Booking,
    *,
    reason: str,
    amount: Optional[float] = None,
    gateway_reference: Optional[str] = None,
) -> None:
    now = utc_now()
    if not booking.refund_requested_at:
        booking.refund_requested_at = now
    booking.refund_status = models.RefundStatus.REFUND_INITIATED
    booking.refund_initiated_at = now
    booking.refund_expected_settlement_at = now + timedelta(days=REFUND_SETTLEMENT_DAYS)
    booking.refund_completed_at = None
    booking.refund_failed_reason = reason
    booking.refund_amount = amount if amount is not None else booking.refund_amount or booking.total_amount
    booking.refund_gateway_reference = gateway_reference or booking.refund_gateway_reference or f"RFND-{uuid.uuid4().hex[:10].upper()}"
    booking.status = models.BookingStatus.CANCELLED


def set_refund_processing(
    booking: models.Booking,
    *,
    reason: str,
    gateway_reference: Optional[str] = None,
) -> None:
    if not booking.refund_requested_at:
        booking.refund_requested_at = utc_now()
    booking.refund_status = models.RefundStatus.REFUND_PROCESSING
    booking.refund_initiated_at = booking.refund_initiated_at or utc_now()
    booking.refund_expected_settlement_at = utc_now() + timedelta(days=REFUND_SETTLEMENT_DAYS)
    booking.refund_failed_reason = reason
    booking.refund_gateway_reference = gateway_reference or booking.refund_gateway_reference
    booking.status = models.BookingStatus.CANCELLED


def set_refund_failed(
    booking: models.Booking,
    *,
    reason: str,
    gateway_reference: Optional[str] = None,
) -> None:
    booking.refund_status = models.RefundStatus.REFUND_FAILED
    booking.refund_failed_reason = reason
    booking.refund_gateway_reference = gateway_reference or booking.refund_gateway_reference
    booking.status = models.BookingStatus.CANCELLED


def set_refund_success(
    booking: models.Booking,
    transaction: models.Transaction,
    *,
    gateway_reference: Optional[str] = None,
) -> None:
    booking.refund_status = models.RefundStatus.REFUND_SUCCESS
    booking.refund_completed_at = utc_now()
    booking.refund_failed_reason = None
    booking.refund_gateway_reference = gateway_reference or booking.refund_gateway_reference
    booking.payment_status = models.PaymentStatus.REFUNDED
    booking.status = models.BookingStatus.CANCELLED
    transaction.status = models.TransactionStatus.REFUNDED
    transaction.failure_reason = None


def set_refund_reversed(
    booking: models.Booking,
    transaction: models.Transaction,
    *,
    reason: str,
) -> None:
    booking.refund_status = models.RefundStatus.REFUND_REVERSED
    booking.refund_failed_reason = reason
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CANCELLED
    transaction.status = models.TransactionStatus.SUCCESS
    transaction.failure_reason = None

def get_booking_or_404(db: Session, booking_id: int) -> models.Booking:
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


def get_success_transaction_for_booking(
    db: Session, booking_id: int
) -> Optional[models.Transaction]:
    return (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking_id,
            models.Transaction.status == models.TransactionStatus.SUCCESS,
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )


def get_latest_transaction_for_booking(
    db: Session, booking_id: int
) -> Optional[models.Transaction]:
    return (
        db.query(models.Transaction)
        .options(joinedload(models.Transaction.booking).joinedload(models.Booking.room))
        .filter(models.Transaction.booking_id == booking_id)
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )


def get_transaction_with_booking(db: Session, transaction_id: int) -> models.Transaction:
    return (
        db.query(models.Transaction)
        .options(joinedload(models.Transaction.booking).joinedload(models.Booking.room))
        .filter(models.Transaction.id == transaction_id)
        .first()
    )


def get_transaction_by_reference(
    db: Session,
    booking_id: int,
    transaction_ref: Optional[str] = None,
    payment_intent_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[models.Transaction]:
    reference_filters = []
    if transaction_ref:
        reference_filters.append(models.Transaction.transaction_ref == transaction_ref)
    if payment_intent_id:
        reference_filters.append(
            models.Transaction.stripe_payment_intent_id == payment_intent_id
        )
    if idempotency_key:
        reference_filters.append(models.Transaction.idempotency_key == idempotency_key)
    if not reference_filters:
        return None
    return (
        db.query(models.Transaction)
        .filter(models.Transaction.booking_id == booking_id, or_(*reference_filters))
        .first()
    )


def get_active_transaction_for_booking(
    db: Session, booking_id: int
) -> Optional[models.Transaction]:
    return (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking_id,
            models.Transaction.status.in_(
                [
                    models.TransactionStatus.PENDING,
                    models.TransactionStatus.PROCESSING,
                ]
            ),
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )


def get_failed_payment_retry_policy(
    db: Session,
    booking_id: int,
    now: Optional[datetime] = None,
) -> dict:
    now = now or utc_now()
    cutoff = now - timedelta(minutes=FAILED_PAYMENT_BLOCK_WINDOW_MINUTES)
    recent_failures = list(
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking_id,
            models.Transaction.status.in_(
                [
                    models.TransactionStatus.FAILED,
                    models.TransactionStatus.EXPIRED,
                ]
            ),
            models.Transaction.created_at >= cutoff,
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .all()
    )
    failed_count = len(recent_failures)
    if failed_count < FAILED_PAYMENT_BLOCK_THRESHOLD:
        return {
            "blocked": False,
            "failed_payment_count": failed_count,
            "retry_after_seconds": 0,
            "retry_available_at": None,
        }

    cooldown_seconds = (
        FAILED_PAYMENT_ESCALATED_BLOCK_SECONDS
        if failed_count >= FAILED_PAYMENT_ESCALATED_BLOCK_THRESHOLD
        else FAILED_PAYMENT_BLOCK_SECONDS
    )
    retry_available_at = recent_failures[0].created_at + timedelta(seconds=cooldown_seconds)
    if retry_available_at.tzinfo is None:
        retry_available_at = retry_available_at.replace(tzinfo=timezone.utc)
    retry_after_seconds = max(0, int((retry_available_at - now).total_seconds()))
    return {
        "blocked": retry_after_seconds > 0,
        "failed_payment_count": failed_count,
        "retry_after_seconds": retry_after_seconds,
        "retry_available_at": retry_available_at.isoformat() if retry_after_seconds > 0 else None,
    }


def has_recent_failed_payment_burst(
    db: Session,
    booking_id: int,
    now: Optional[datetime] = None,
) -> bool:
    return bool(get_failed_payment_retry_policy(db, booking_id, now)["blocked"])


def ensure_booking_can_accept_payment(db: Session, booking: models.Booking) -> models.Booking:
    release_expired_holds(db, booking_id=booking.id)
    db.refresh(booking)
    if expire_stale_booking_hold(booking):
        booking.payment_status = models.PaymentStatus.EXPIRED
        booking.status = models.BookingStatus.EXPIRED
        db.commit()
        db.refresh(booking)

    if booking.status in [models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED]:
        raise HTTPException(
            status_code=400, detail="Cancelled or expired bookings cannot be paid"
        )
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(status_code=409, detail="Booking already paid")
    if get_success_transaction_for_booking(db, booking.id):
        raise HTTPException(status_code=409, detail="Booking already paid")
    retry_policy = get_failed_payment_retry_policy(db, booking.id)
    if retry_policy["blocked"]:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "PAYMENT_RETRY_COOLDOWN",
                "message": "Payment temporarily paused for security.",
                **retry_policy,
            },
        )

    # Revalidate that THIS booking still holds its inventory lock.
    # This closes the gap where an external cleanup process released the lock
    # while the booking hold window was still open.
    if not is_booking_inventory_locked(db, booking=booking):
        now = datetime.now(timezone.utc)
        hold_exp = booking.hold_expires_at
        if hold_exp is not None and hold_exp.tzinfo is None:
            hold_exp = hold_exp.replace(tzinfo=timezone.utc)
        if hold_exp and hold_exp > now:
            # Hold is still valid but the inventory row was released externally.
            # Try to re-acquire the lock and continue.
            try:
                lock_inventory_for_booking(db, booking=booking, lock_expires_at=hold_exp)
                db.commit()
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Dates are no longer available — inventory was released",
                ) from exc
        else:
            raise HTTPException(
                status_code=409,
                detail="Booking hold has expired — please start a new booking",
            )

    return booking


def apply_processing_state(booking: models.Booking) -> None:
    booking.payment_status = models.PaymentStatus.PROCESSING
    if booking.status == models.BookingStatus.PENDING:
        booking.status = models.BookingStatus.PROCESSING


def apply_paid_state(booking: models.Booking) -> None:
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED


def apply_failed_state(booking: models.Booking) -> None:
    if booking.payment_status != models.PaymentStatus.PAID:
        booking.payment_status = models.PaymentStatus.FAILED
        if booking.status not in [models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED]:
            booking.status = models.BookingStatus.PENDING


def apply_expired_state(booking: models.Booking) -> None:
    if booking.payment_status != models.PaymentStatus.PAID:
        booking.payment_status = models.PaymentStatus.EXPIRED
        if booking.status != models.BookingStatus.CANCELLED:
            booking.status = models.BookingStatus.EXPIRED


def get_card_details(payment_intent: dict) -> tuple[Optional[str], Optional[str]]:
    charges = payment_intent.get("charges", {}).get("data", [])
    if not charges:
        return None, None
    card = charges[0].get("payment_method_details", {}).get("card", {})
    return card.get("last4"), card.get("brand")


def verify_card_payment_intent_succeeded(
    payment_intent_id: Optional[str],
    *,
    attempts: int = 1,
    delay_seconds: float = 0.0,
) -> tuple[bool, Optional[str], Optional[str]]:
    if not payment_intent_id or not settings.stripe_secret_key:
        return False, None, None

    stripe.api_key = settings.stripe_secret_key
    safe_attempts = max(1, attempts)
    for attempt in range(safe_attempts):
        try:
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        except Exception:
            payment_intent = None

        if payment_intent is not None:
            status = (
                payment_intent.get("status")
                if isinstance(payment_intent, dict)
                else getattr(payment_intent, "status", None)
            )
            if status == "succeeded":
                last4, brand = get_card_details(payment_intent)
                return True, last4, brand

        if attempt < safe_attempts - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

    return False, None, None


def build_payment_intent_response(
    transaction: models.Transaction,
    booking: models.Booking,
    mode: str,
) -> dict:
    return {
        "client_secret": transaction.provider_client_secret,
        "payment_intent_id": transaction.stripe_payment_intent_id,
        "transaction_ref": transaction.transaction_ref,
        "amount": booking.total_amount,
        "currency": "usd",
        "booking_ref": booking.booking_ref,
        "mode": mode,
        "status": transaction.status.value,
        "idempotency_key": transaction.idempotency_key,
    }


def create_pending_transaction(
    db: Session,
    booking: models.Booking,
    payment_method: str,
    idempotency_key: str,
    retry_of_transaction_id: Optional[int] = None,
) -> models.Transaction:
    transaction = models.Transaction(
        booking_id=booking.id,
        transaction_ref=generate_txn_ref(),
        idempotency_key=idempotency_key,
        amount=booking.total_amount,
        currency="USD",
        payment_method=payment_method,
        status=models.TransactionStatus.PENDING,
        retry_of_transaction_id=retry_of_transaction_id,
    )
    db.add(transaction)
    db.flush()
    write_payment_audit(
        db,
        action="payment.intent.created",
        booking=booking,
        transaction=transaction,
        metadata={
            "payment_method": payment_method,
            "idempotency_key": idempotency_key,
            "retry_of_transaction_id": retry_of_transaction_id,
        },
    )
    return transaction


def upsert_success_transaction(
    db: Session,
    booking: models.Booking,
    transaction_ref: str,
    payment_method: str,
    payment_intent_id: Optional[str] = None,
    card_last4: Optional[str] = None,
    card_brand: Optional[str] = None,
) -> models.Transaction:
    existing_success = get_success_transaction_for_booking(db, booking.id)
    if existing_success:
        apply_paid_state(booking)
        write_payment_audit(
            db,
            action="payment.success.idempotent",
            booking=booking,
            transaction=existing_success,
            metadata={"transaction_ref": existing_success.transaction_ref},
        )
        db.commit()
        return get_transaction_with_booking(db, existing_success.id)

    transaction = get_transaction_by_reference(
        db,
        booking_id=booking.id,
        transaction_ref=transaction_ref,
        payment_intent_id=payment_intent_id,
    )
    if not transaction:
        transaction = models.Transaction(
            booking_id=booking.id,
            transaction_ref=transaction_ref,
            stripe_payment_intent_id=payment_intent_id,
            amount=booking.total_amount,
            currency="USD",
            payment_method=payment_method,
        )
        db.add(transaction)

    transaction.transaction_ref = transaction_ref
    transaction.stripe_payment_intent_id = payment_intent_id
    transaction.amount = booking.total_amount
    transaction.currency = "USD"
    transaction.payment_method = payment_method
    transaction.card_last4 = card_last4
    transaction.card_brand = card_brand
    transaction.status = models.TransactionStatus.SUCCESS
    transaction.failure_reason = None

    apply_paid_state(booking)
    confirm_inventory_for_booking(db, booking=booking)
    db.flush()
    queue_booking_confirmation_email(db, booking, transaction)
    queue_payment_receipt_email(db, booking, transaction)
    write_payment_audit(
        db,
        action="payment.success.recorded",
        booking=booking,
        transaction=transaction,
        metadata={
            "payment_method": payment_method,
            "payment_intent_id": payment_intent_id,
        },
    )

    db.commit()
    db.refresh(transaction)
    resolved = get_transaction_with_booking(db, transaction.id)
    attach_transaction_lifecycle_state(db, resolved)
    return resolved


def get_successful_or_processing_response(
    db: Session,
    booking: models.Booking,
    transaction_ref: str,
    payment_method: str,
    payment_intent_id: Optional[str] = None,
    card_last4: Optional[str] = None,
    card_brand: Optional[str] = None,
) -> models.Transaction:
    if payment_method == "mock":
        return upsert_success_transaction(
            db=db,
            booking=booking,
            transaction_ref=transaction_ref,
            payment_method=payment_method,
            payment_intent_id=payment_intent_id,
            card_last4=card_last4,
            card_brand=card_brand,
        )

    existing_success = get_success_transaction_for_booking(db, booking.id)
    if existing_success:
        resolved = get_transaction_with_booking(db, existing_success.id)
        attach_transaction_lifecycle_state(db, resolved)
        return resolved

    confirmed, verified_last4, verified_brand = verify_card_payment_intent_succeeded(
        payment_intent_id,
        attempts=3,
        delay_seconds=0.5,
    )
    if confirmed:
        return upsert_success_transaction(
            db=db,
            booking=booking,
            transaction_ref=transaction_ref,
            payment_method=payment_method,
            payment_intent_id=payment_intent_id,
            card_last4=verified_last4 or card_last4,
            card_brand=verified_brand or card_brand,
        )

    return mark_transaction_processing(
        db=db,
        booking=booking,
        transaction_ref=transaction_ref,
        payment_method=payment_method,
        payment_intent_id=payment_intent_id,
        card_last4=card_last4,
        card_brand=card_brand,
    )


def mark_transaction_processing(
    db: Session,
    booking: models.Booking,
    transaction_ref: str,
    payment_method: str,
    payment_intent_id: Optional[str] = None,
    card_last4: Optional[str] = None,
    card_brand: Optional[str] = None,
) -> models.Transaction:
    transaction = get_transaction_by_reference(
        db,
        booking_id=booking.id,
        transaction_ref=transaction_ref,
        payment_intent_id=payment_intent_id,
    )
    if not transaction:
        transaction = models.Transaction(
            booking_id=booking.id,
            transaction_ref=transaction_ref,
            stripe_payment_intent_id=payment_intent_id,
            amount=booking.total_amount,
            currency="USD",
            payment_method=payment_method,
            status=models.TransactionStatus.PENDING,
        )
        db.add(transaction)

    transaction.transaction_ref = transaction_ref
    transaction.stripe_payment_intent_id = payment_intent_id
    transaction.payment_method = payment_method
    transaction.card_last4 = card_last4
    transaction.card_brand = card_brand
    transaction.status = models.TransactionStatus.PROCESSING
    transaction.failure_reason = None
    apply_processing_state(booking)
    write_payment_audit(
        db,
        action="payment.processing.recorded",
        booking=booking,
        transaction=transaction,
        metadata={
            "payment_method": payment_method,
            "payment_intent_id": payment_intent_id,
        },
    )

    db.commit()
    db.refresh(transaction)
    resolved = get_transaction_with_booking(db, transaction.id)
    attach_transaction_lifecycle_state(db, resolved)
    return resolved


def record_failed_transaction(
    db: Session,
    booking: models.Booking,
    reason: str,
    payment_method: str = "card",
    payment_intent_id: Optional[str] = None,
    transaction_ref: Optional[str] = None,
) -> models.Transaction:
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(status_code=409, detail="Booking is already paid")

    transaction = get_transaction_by_reference(
        db,
        booking_id=booking.id,
        payment_intent_id=payment_intent_id,
        transaction_ref=transaction_ref,
    )
    if not transaction:
        transaction = models.Transaction(
            booking_id=booking.id,
            transaction_ref=transaction_ref or generate_txn_ref(),
            stripe_payment_intent_id=payment_intent_id,
            amount=booking.total_amount,
            currency="USD",
            payment_method=payment_method,
        )
        db.add(transaction)

    transaction.amount = booking.total_amount
    transaction.currency = "USD"
    transaction.payment_method = payment_method
    transaction.status = models.TransactionStatus.FAILED
    transaction.failure_reason = reason

    apply_failed_state(booking)
    # Only release inventory if the hold window has already expired.
    # While the hold is still valid the user can retry with a different card —
    # keeping the lock means no extend-hold call is needed within the window.
    now = utc_now()
    hold_exp = booking.hold_expires_at
    if hold_exp is not None and hold_exp.tzinfo is None:
        hold_exp = hold_exp.replace(tzinfo=timezone.utc)
    if not hold_exp or hold_exp <= now:
        release_inventory_for_booking(db, booking=booking)
    # else: hold still valid — keep the lock so the user can retry immediately
    db.flush()
    queue_payment_failure_email(db, booking, transaction, reason)
    write_payment_audit(
        db,
        action="payment.failure.recorded",
        booking=booking,
        transaction=transaction,
        metadata={
            "reason": reason,
            "payment_intent_id": payment_intent_id,
            "transaction_ref": transaction.transaction_ref,
        },
    )

    db.commit()
    db.refresh(transaction)
    return transaction


def reconcile_stuck_payments(
    db: Session,
    now: Optional[datetime] = None,
    timeout_minutes: int = RECONCILIATION_TIMEOUT_MINUTES,
) -> int:
    now = now or utc_now()
    timeout_cutoff = now - timedelta(minutes=timeout_minutes)
    stale_transactions = (
        db.query(models.Transaction)
        .options(joinedload(models.Transaction.booking))
        .filter(
            models.Transaction.status.in_(
                [models.TransactionStatus.PENDING, models.TransactionStatus.PROCESSING]
            ),
            models.Transaction.created_at <= timeout_cutoff,
        )
        .all()
    )

    updated = 0
    for transaction in stale_transactions:
        booking = transaction.booking
        if get_success_transaction_for_booking(db, booking.id):
            continue
        transaction.status = models.TransactionStatus.EXPIRED
        transaction.failure_reason = "Payment attempt expired before confirmation"
        apply_expired_state(booking)
        release_inventory_for_booking(db, booking=booking)
        updated += 1

    if updated:
        db.commit()
    return updated


def reconcile_payment_integrity(db: Session) -> dict[str, int]:
    recovered_success_states = 0
    orphan_paid_bookings = 0

    success_transactions = (
        db.query(models.Transaction)
        .options(joinedload(models.Transaction.booking))
        .filter(models.Transaction.status == models.TransactionStatus.SUCCESS)
        .all()
    )
    for transaction in success_transactions:
        booking = transaction.booking
        if not booking:
            continue
        if (
            booking.payment_status != models.PaymentStatus.PAID
            or booking.status != models.BookingStatus.CONFIRMED
        ):
            apply_paid_state(booking)
            confirm_inventory_for_booking(db, booking=booking)
            write_payment_audit(
                db,
                action="payments.reconcile.recovered-success-state",
                booking=booking,
                transaction=transaction,
            )
            recovered_success_states += 1

    paid_bookings = (
        db.query(models.Booking)
        .filter(models.Booking.payment_status == models.PaymentStatus.PAID)
        .all()
    )
    for booking in paid_bookings:
        success_transaction = get_success_transaction_for_booking(db, booking.id)
        if success_transaction:
            continue
        write_payment_audit(
            db,
            action="payments.reconcile.orphan-paid-booking",
            booking=booking,
        )
        queue_payment_ops_alert(
            db,
            subject=f"Orphan paid booking detected: {booking.booking_ref}",
            body=(
                f"Booking {booking.booking_ref} is marked paid but has no successful "
                "transaction record. Manual finance review is required."
            ),
            booking=booking,
        )
        orphan_paid_bookings += 1

    if recovered_success_states or orphan_paid_bookings:
        db.commit()
    return {
        "recovered_success_states": recovered_success_states,
        "orphan_paid_bookings": orphan_paid_bookings,
    }


@router.post("/create-payment-intent")
def create_payment_intent(
    payload: schemas.CreatePaymentIntent,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit("payments:create-intent", request, subject=str(payload.booking_id))
    booking = ensure_booking_can_accept_payment(db, get_booking_or_404(db, payload.booking_id))
    idempotency_key = payload.idempotency_key or f"pay_{uuid.uuid4().hex}"

    existing_attempt = get_transaction_by_reference(
        db,
        booking_id=booking.id,
        idempotency_key=idempotency_key,
    )
    if existing_attempt:
        mode = "mock" if existing_attempt.payment_method == "mock" else "stripe"
        return build_payment_intent_response(existing_attempt, booking, mode)

    active_transaction = get_active_transaction_for_booking(db, booking.id)
    if active_transaction:
        raise HTTPException(
            status_code=409,
            detail="A payment attempt is already in progress for this booking",
        )

    latest_transaction = get_latest_transaction_for_booking(db, booking.id)
    retry_of_transaction_id = None
    if latest_transaction and latest_transaction.status in [
        models.TransactionStatus.FAILED,
        models.TransactionStatus.EXPIRED,
    ]:
        retry_of_transaction_id = latest_transaction.id

    transaction = create_pending_transaction(
        db=db,
        booking=booking,
        payment_method=payload.payment_method,
        idempotency_key=idempotency_key,
        retry_of_transaction_id=retry_of_transaction_id,
    )

    if payload.payment_method == "mock":
        transaction.stripe_payment_intent_id = f"pi_mock_{uuid.uuid4().hex[:16]}"
        transaction.provider_client_secret = f"mock_{transaction.transaction_ref}"
        db.commit()
        db.refresh(transaction)
        return build_payment_intent_response(transaction, booking, "mock")

    stripe.api_key = settings.stripe_secret_key
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(booking.total_amount * 100),
            currency="usd",
            metadata={
                "booking_id": str(booking.id),
                "booking_ref": booking.booking_ref,
                "email": booking.email,
                "transaction_ref": transaction.transaction_ref,
                "idempotency_key": idempotency_key,
            },
            receipt_email=booking.email,
            idempotency_key=idempotency_key,
            payment_method_options={
                "card": {
                    # Request bank-side 3DS/OTP whenever supported. Stripe and the
                    # issuer still own the actual challenge UX and final decision.
                    "request_three_d_secure": "any",
                }
            },
        )
        transaction.stripe_payment_intent_id = intent.id
        transaction.provider_client_secret = intent.client_secret
        db.commit()
        db.refresh(transaction)
        return build_payment_intent_response(transaction, booking, "stripe")
    except Exception as exc:
        transaction.status = models.TransactionStatus.FAILED
        message = getattr(exc, "user_message", None) or str(exc)
        transaction.failure_reason = message
        apply_failed_state(booking)
        db.commit()
        raise HTTPException(status_code=400, detail=message) from exc


@router.post("/payment-success", response_model=schemas.TransactionResponse)
def confirm_payment_success(
    payload: schemas.PaymentSuccess,
    db: Session = Depends(get_db),
):
    booking = get_booking_or_404(db, payload.booking_id)
    if booking.payment_status == models.PaymentStatus.PAID:
        existing_success = get_success_transaction_for_booking(db, booking.id)
        if existing_success:
            resolved = get_transaction_with_booking(db, existing_success.id)
            attach_transaction_lifecycle_state(db, resolved)
            return resolved
    booking = ensure_booking_can_accept_payment(db, booking)
    return get_successful_or_processing_response(
        db=db,
        booking=booking,
        transaction_ref=payload.transaction_ref,
        payment_method=payload.payment_method,
        payment_intent_id=payload.payment_intent_id,
        card_last4=payload.card_last4,
        card_brand=payload.card_brand,
    )


@router.post("/payment-failure")
def record_payment_failure(
    request: Request,
    booking_id: int,
    reason: str = "Payment declined",
    payment_intent_id: Optional[str] = None,
    transaction_ref: Optional[str] = None,
    db: Session = Depends(get_db),
):
    enforce_rate_limit("payments:failure", request, subject=str(booking_id))
    booking = get_booking_or_404(db, booking_id)
    release_expired_holds(db, booking_id=booking.id)
    db.refresh(booking)
    if booking.status in [models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED]:
        raise HTTPException(
            status_code=400, detail="Cancelled or expired bookings cannot be updated"
        )
    if expire_stale_booking_hold(booking):
        apply_expired_state(booking)
        db.commit()
        db.refresh(booking)
        raise HTTPException(
            status_code=400, detail="Cancelled or expired bookings cannot be updated"
        )
    transaction = record_failed_transaction(
        db=db,
        booking=booking,
        reason=reason,
        payment_intent_id=payment_intent_id,
        transaction_ref=transaction_ref,
    )
    retry_policy = get_failed_payment_retry_policy(db, booking.id)
    return {
        "message": "Payment failure recorded",
        "transaction_ref": transaction.transaction_ref,
        **retry_policy,
    }


@router.get("/status/{booking_id}", response_model=schemas.PaymentStateResponse)
def get_payment_status(booking_id: int, db: Session = Depends(get_db)):
    booking = get_booking_or_404(db, booking_id)
    if expire_stale_booking_hold(booking):
        apply_expired_state(booking)
        db.commit()
        db.refresh(booking)
    elif reconcile_gateway_payment_state(db, booking, attempts=2, delay_seconds=0.25):
        db.commit()
        db.refresh(booking)
    latest_transaction = get_latest_transaction_for_booking(db, booking_id)
    lifecycle_state = attach_booking_lifecycle_state(db, booking, latest_transaction)
    attach_transaction_lifecycle_state(db, latest_transaction)
    retry_policy = get_failed_payment_retry_policy(db, booking.id)
    return schemas.PaymentStateResponse(
        booking_id=booking.id,
        booking_ref=booking.booking_ref,
        booking_status=booking.status,
        payment_status=booking.payment_status,
        lifecycle_state=lifecycle_state,
        latest_transaction=latest_transaction,
        failed_payment_count=retry_policy["failed_payment_count"],
        retry_after_seconds=retry_policy["retry_after_seconds"],
        retry_available_at=retry_policy["retry_available_at"],
    )


@router.post("/reconcile-stuck")
def reconcile_stuck_payment_attempts(
    timeout_minutes: int = Query(RECONCILIATION_TIMEOUT_MINUTES, ge=1, le=1440),
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    updated = reconcile_stuck_payments(db, timeout_minutes=timeout_minutes)
    integrity = reconcile_payment_integrity(db)
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="payments.reconcile",
        entity_type="payment",
        entity_id="stuck-attempts",
        metadata={
            "timeout_minutes": timeout_minutes,
            "updated": updated,
            **integrity,
        },
    )
    db.commit()
    return {
        "message": "Reconciliation completed",
        "updated": updated,
        **integrity,
    }


@router.get("/transactions", response_model=schemas.TransactionListResponse)
def get_transactions(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(10),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    query = db.query(models.Transaction).options(
        joinedload(models.Transaction.booking).joinedload(models.Booking.room)
    )
    if status:
        query = query.filter(models.Transaction.status == status)

    total = query.count()
    transactions = (
        query.order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {"transactions": transactions, "total": total}


@router.get("/transactions/{txn_id}", response_model=schemas.TransactionResponse)
def get_transaction(
    txn_id: int,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    txn = (
        db.query(models.Transaction)
        .options(joinedload(models.Transaction.booking).joinedload(models.Booking.room))
        .filter(models.Transaction.id == txn_id)
        .first()
    )
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn


@router.get("/refunds/{booking_id}", response_model=schemas.RefundTimelineResponse)
def get_refund_timeline(
    booking_id: int,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    booking = get_booking_or_404(db, booking_id)
    return build_refund_timeline(booking)


@router.post("/refunds/request", response_model=schemas.RefundAdminActionResponse)
def request_refund(
    payload: schemas.RefundRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking, transaction = get_refundable_booking_or_404(db, payload.booking_id)
    ensure_refund_status(
        booking,
        {None, models.RefundStatus.REFUND_FAILED, models.RefundStatus.REFUND_REVERSED},
        "Refund request is already active for this booking",
    )
    set_refund_requested(booking, reason=payload.reason)
    write_payment_audit(
        db,
        actor_user_id=admin.id,
        action="payment.refund.requested",
        booking=booking,
        transaction=transaction,
        metadata={"reason": payload.reason},
    )
    db.commit()
    db.refresh(booking)
    return schemas.RefundAdminActionResponse(
        message="Refund requested successfully",
        timeline=build_refund_timeline(booking),
    )


@router.post("/refunds/{booking_id}/initiate", response_model=schemas.RefundAdminActionResponse)
def initiate_refund(
    booking_id: int,
    payload: schemas.RefundAdminActionRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking, transaction = get_refundable_booking_or_404(db, booking_id)
    ensure_refund_status(
        booking,
        {None, models.RefundStatus.REFUND_REQUESTED, models.RefundStatus.REFUND_FAILED, models.RefundStatus.REFUND_REVERSED},
        "Refund can only be initiated from a requested, failed, or reversed state",
    )
    set_refund_initiated(
        booking,
        reason=payload.reason,
        amount=payload.amount,
        gateway_reference=payload.gateway_reference,
    )
    release_inventory_for_booking(db, booking=booking)
    queue_booking_cancellation_email(db, booking)
    queue_refund_initiated_email(db, booking)
    write_payment_audit(
        db,
        actor_user_id=admin.id,
        action="payment.refund.initiated",
        booking=booking,
        transaction=transaction,
        metadata={
            "reason": payload.reason,
            "gateway_reference": booking.refund_gateway_reference,
        },
    )
    db.commit()
    db.refresh(booking)
    return schemas.RefundAdminActionResponse(
        message="Refund initiated successfully",
        timeline=build_refund_timeline(booking),
    )


@router.post("/refunds/{booking_id}/retry", response_model=schemas.RefundAdminActionResponse)
def retry_failed_refund(
    booking_id: int,
    payload: schemas.RefundAdminActionRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking, transaction = get_refundable_booking_or_404(db, booking_id)
    ensure_refund_status(
        booking,
        {models.RefundStatus.REFUND_FAILED},
        "Only failed refunds can be retried",
    )
    set_refund_processing(
        booking,
        reason=payload.reason,
        gateway_reference=payload.gateway_reference,
    )
    queue_refund_initiated_email(db, booking)
    write_payment_audit(
        db,
        actor_user_id=admin.id,
        action="payment.refund.retried",
        booking=booking,
        transaction=transaction,
        metadata={"reason": payload.reason},
    )
    db.commit()
    db.refresh(booking)
    return schemas.RefundAdminActionResponse(
        message="Refund retry started",
        timeline=build_refund_timeline(booking),
    )


@router.post("/refunds/{booking_id}/fail", response_model=schemas.RefundAdminActionResponse)
def fail_refund(
    booking_id: int,
    payload: schemas.RefundAdminActionRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking, transaction = get_refundable_booking_or_404(db, booking_id)
    ensure_refund_status(
        booking,
        {
            models.RefundStatus.REFUND_REQUESTED,
            models.RefundStatus.REFUND_INITIATED,
            models.RefundStatus.REFUND_PROCESSING,
        },
        "Only active refund requests can be marked failed",
    )
    set_refund_failed(
        booking,
        reason=payload.reason,
        gateway_reference=payload.gateway_reference,
    )
    queue_refund_failure_email(db, booking)
    write_payment_audit(
        db,
        actor_user_id=admin.id,
        action="payment.refund.failed",
        booking=booking,
        transaction=transaction,
        metadata={"reason": payload.reason},
    )
    db.commit()
    db.refresh(booking)
    return schemas.RefundAdminActionResponse(
        message="Refund marked as failed",
        timeline=build_refund_timeline(booking),
    )


@router.post("/refunds/{booking_id}/complete", response_model=schemas.RefundAdminActionResponse)
def complete_refund(
    booking_id: int,
    payload: schemas.RefundAdminActionRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking, transaction = get_refundable_booking_or_404(db, booking_id)
    ensure_refund_status(
        booking,
        {
            models.RefundStatus.REFUND_REQUESTED,
            models.RefundStatus.REFUND_INITIATED,
            models.RefundStatus.REFUND_PROCESSING,
            models.RefundStatus.REFUND_FAILED,
        },
        "Refund can only be completed from an active or failed refund state",
    )
    if not booking.refund_initiated_at:
        set_refund_initiated(
            booking,
            reason=payload.reason,
            amount=payload.amount,
            gateway_reference=payload.gateway_reference,
        )
    set_refund_success(
        booking,
        transaction,
        gateway_reference=payload.gateway_reference,
    )
    queue_refund_success_email(db, booking)
    write_payment_audit(
        db,
        actor_user_id=admin.id,
        action="payment.refund.completed",
        booking=booking,
        transaction=transaction,
        metadata={"gateway_reference": booking.refund_gateway_reference},
    )
    db.commit()
    db.refresh(booking)
    return schemas.RefundAdminActionResponse(
        message="Refund completed successfully",
        timeline=build_refund_timeline(booking),
    )


@router.post("/refunds/{booking_id}/reverse", response_model=schemas.RefundAdminActionResponse)
def reverse_refund(
    booking_id: int,
    payload: schemas.RefundAdminActionRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking, transaction = get_refundable_booking_or_404(db, booking_id)
    ensure_refund_status(
        booking,
        {models.RefundStatus.REFUND_SUCCESS},
        "Only completed refunds can be reversed",
    )
    set_refund_reversed(booking, transaction, reason=payload.reason)
    write_payment_audit(
        db,
        actor_user_id=admin.id,
        action="payment.refund.reversed",
        booking=booking,
        transaction=transaction,
        metadata={"reason": payload.reason},
    )
    db.commit()
    db.refresh(booking)
    return schemas.RefundAdminActionResponse(
        message="Refund reversed successfully",
        timeline=build_refund_timeline(booking),
    )


@router.post("/refund")
def refund_payment(
    payload: schemas.RefundRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    response = initiate_refund(
        booking_id=payload.booking_id,
        payload=schemas.RefundAdminActionRequest(reason=payload.reason),
        db=db,
        admin=admin,
    )
    return {
        "message": "Refund recorded successfully",
        "booking_id": response.timeline.booking_id,
        "transaction_ref": response.timeline.gateway_reference,
        "refund_status": response.timeline.refund_status.value,
    }


@router.get("/admin/reconciliation")
def get_payment_reconciliation_dashboard(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    pending = (
        db.query(models.Transaction)
        .filter(models.Transaction.status == models.TransactionStatus.PENDING)
        .count()
    )
    processing = (
        db.query(models.Transaction)
        .filter(models.Transaction.status == models.TransactionStatus.PROCESSING)
        .count()
    )
    failed = (
        db.query(models.Transaction)
        .filter(models.Transaction.status == models.TransactionStatus.FAILED)
        .count()
    )
    refunded = (
        db.query(models.Transaction)
        .filter(models.Transaction.status == models.TransactionStatus.REFUNDED)
        .count()
    )
    expired = (
        db.query(models.Transaction)
        .filter(models.Transaction.status == models.TransactionStatus.EXPIRED)
        .count()
    )

    recent_failures = (
        db.query(models.Transaction)
        .options(joinedload(models.Transaction.booking))
        .filter(
            models.Transaction.status.in_(
                [
                    models.TransactionStatus.FAILED,
                    models.TransactionStatus.EXPIRED,
                ]
            )
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .limit(10)
        .all()
    )

    return {
        "pending_attempts": pending,
        "processing_attempts": processing,
        "failed_attempts": failed,
        "refunded_attempts": refunded,
        "expired_attempts": expired,
        "recent_failures": [
            {
                "transaction_ref": txn.transaction_ref,
                "status": txn.status.value,
                "booking_ref": txn.booking.booking_ref if txn.booking else None,
                "failure_reason": txn.failure_reason,
            }
            for txn in recent_failures
        ],
    }


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    stripe.api_key = settings.stripe_secret_key

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    except stripe.error.SignatureVerificationError as exc:
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    if event["type"] == "payment_intent.succeeded":
        payment_intent = event["data"]["object"]
        metadata = payment_intent.get("metadata", {})
        booking_id_str = metadata.get("booking_id")
        transaction_ref = metadata.get("transaction_ref")

        if not booking_id_str:
            return {"status": "ok", "note": "no booking_id in metadata"}

        booking = db.query(models.Booking).filter(
            models.Booking.id == int(booking_id_str)
        ).first()
        if not booking or booking.payment_status == models.PaymentStatus.PAID:
            return {"status": "ok", "note": "already processed"}

        transaction: Optional[models.Transaction] = None
        if transaction_ref:
            transaction = db.query(models.Transaction).filter(
                models.Transaction.transaction_ref == transaction_ref
            ).first()
        if not transaction:
            transaction = get_latest_transaction_for_booking(db, booking.id)

        if transaction and transaction.status != models.TransactionStatus.SUCCESS:
            transaction.status = models.TransactionStatus.SUCCESS
            last4, brand = get_card_details(payment_intent)
            if last4:
                transaction.card_last4 = last4
            if brand:
                transaction.card_brand = brand

        booking.payment_status = models.PaymentStatus.PAID
        booking.status = models.BookingStatus.CONFIRMED
        confirm_inventory_for_booking(db, booking=booking)

        if transaction:
            if not _notification_exists(db, booking_id=booking.id, transaction_id=transaction.id, event_type="booking_confirmed"):
                queue_booking_confirmation_email(db, booking, transaction)
            if not _notification_exists(db, booking_id=booking.id, transaction_id=transaction.id, event_type="payment_receipt"):
                queue_payment_receipt_email(db, booking, transaction)

        db.commit()
        write_payment_audit(db, booking_id=booking.id, action="webhook_confirmed",
                            actor_id=None, notes=f"Stripe webhook confirmed payment_intent {payment_intent.get('id')}")

    elif event["type"] == "payment_intent.payment_failed":
        payment_intent = event["data"]["object"]
        metadata = payment_intent.get("metadata", {})
        booking_id_str = metadata.get("booking_id")
        transaction_ref = metadata.get("transaction_ref")

        if booking_id_str:
            booking = db.query(models.Booking).filter(
                models.Booking.id == int(booking_id_str)
            ).first()
            if booking and booking.payment_status not in (
                models.PaymentStatus.PAID, models.PaymentStatus.FAILED
            ):
                transaction = None
                if transaction_ref:
                    transaction = db.query(models.Transaction).filter(
                        models.Transaction.transaction_ref == transaction_ref
                    ).first()
                if transaction and transaction.status == models.TransactionStatus.PENDING:
                    err = payment_intent.get("last_payment_error", {})
                    transaction.status = models.TransactionStatus.FAILED
                    transaction.failure_reason = err.get("message", "Payment failed")
                    booking.payment_status = models.PaymentStatus.FAILED
                    queue_payment_failure_email(db, booking, transaction)
                    db.commit()

    return {"status": "ok"}
