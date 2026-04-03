import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db, settings
from routers.auth import get_current_admin
from routers.bookings import expire_stale_booking_hold, release_expired_holds
from services.audit_service import write_audit_log
from services.inventory_service import confirm_inventory_for_booking, release_inventory_for_booking
from services.notification_service import (
    queue_booking_cancellation_email,
    queue_booking_confirmation_email,
    queue_payment_failure_email,
    queue_payment_receipt_email,
)
from services.rate_limit_service import enforce_rate_limit

router = APIRouter(prefix="/payments", tags=["Payments"])

RECONCILIATION_TIMEOUT_MINUTES = 30
FAILED_PAYMENT_BLOCK_WINDOW_MINUTES = 30
FAILED_PAYMENT_BLOCK_THRESHOLD = 3


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_txn_ref() -> str:
    return "TXN-" + str(uuid.uuid4()).upper()[:12]


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


def has_recent_failed_payment_burst(
    db: Session,
    booking_id: int,
    now: Optional[datetime] = None,
) -> bool:
    now = now or utc_now()
    cutoff = now - timedelta(minutes=FAILED_PAYMENT_BLOCK_WINDOW_MINUTES)
    recent_failures = (
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
        .count()
    )
    return recent_failures >= FAILED_PAYMENT_BLOCK_THRESHOLD


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
    if has_recent_failed_payment_burst(db, booking.id):
        raise HTTPException(
            status_code=429,
            detail="Payment attempts temporarily blocked due to repeated failures",
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

    db.commit()
    db.refresh(transaction)
    return get_transaction_with_booking(db, transaction.id)


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

    db.commit()
    db.refresh(transaction)
    return get_transaction_with_booking(db, transaction.id)


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
    release_inventory_for_booking(db, booking=booking)
    db.flush()
    queue_payment_failure_email(db, booking, transaction, reason)

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
        )
        transaction.stripe_payment_intent_id = intent.id
        transaction.provider_client_secret = intent.client_secret
        db.commit()
        db.refresh(transaction)
        return build_payment_intent_response(transaction, booking, "stripe")
    except stripe.error.StripeError as exc:
        transaction.status = models.TransactionStatus.FAILED
        transaction.failure_reason = str(exc.user_message)
        apply_failed_state(booking)
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc.user_message))


@router.post("/payment-success", response_model=schemas.TransactionResponse)
def confirm_payment_success(
    payload: schemas.PaymentSuccess,
    db: Session = Depends(get_db),
):
    booking = ensure_booking_can_accept_payment(db, get_booking_or_404(db, payload.booking_id))

    if payload.payment_method == "mock":
        return upsert_success_transaction(
            db=db,
            booking=booking,
            transaction_ref=payload.transaction_ref,
            payment_method=payload.payment_method,
            payment_intent_id=payload.payment_intent_id,
            card_last4=payload.card_last4,
            card_brand=payload.card_brand,
        )

    return mark_transaction_processing(
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
    return {"message": "Payment failure recorded", "transaction_ref": transaction.transaction_ref}


@router.get("/status/{booking_id}", response_model=schemas.PaymentStateResponse)
def get_payment_status(booking_id: int, db: Session = Depends(get_db)):
    booking = get_booking_or_404(db, booking_id)
    if expire_stale_booking_hold(booking):
        apply_expired_state(booking)
        db.commit()
        db.refresh(booking)
    latest_transaction = get_latest_transaction_for_booking(db, booking_id)
    return schemas.PaymentStateResponse(
        booking_id=booking.id,
        booking_ref=booking.booking_ref,
        booking_status=booking.status,
        payment_status=booking.payment_status,
        latest_transaction=latest_transaction,
    )


@router.post("/reconcile-stuck")
def reconcile_stuck_payment_attempts(
    timeout_minutes: int = Query(RECONCILIATION_TIMEOUT_MINUTES, ge=1, le=1440),
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    updated = reconcile_stuck_payments(db, timeout_minutes=timeout_minutes)
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="payments.reconcile",
        entity_type="payment",
        entity_id="stuck-attempts",
        metadata={"timeout_minutes": timeout_minutes, "updated": updated},
    )
    db.commit()
    return {"message": "Reconciliation completed", "updated": updated}


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
def get_transaction(txn_id: int, db: Session = Depends(get_db)):
    txn = (
        db.query(models.Transaction)
        .options(joinedload(models.Transaction.booking).joinedload(models.Booking.room))
        .filter(models.Transaction.id == txn_id)
        .first()
    )
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn


@router.post("/refund")
def refund_payment(
    payload: schemas.RefundRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking = get_booking_or_404(db, payload.booking_id)
    if booking.payment_status != models.PaymentStatus.PAID:
        raise HTTPException(status_code=400, detail="Only paid bookings can be refunded")

    transaction = get_success_transaction_for_booking(db, booking.id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Successful transaction not found")

    transaction.status = models.TransactionStatus.REFUNDED
    transaction.failure_reason = payload.reason
    booking.payment_status = models.PaymentStatus.REFUNDED
    booking.status = models.BookingStatus.CANCELLED
    release_inventory_for_booking(db, booking=booking)
    queue_booking_cancellation_email(db, booking)
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="payment.refund",
        entity_type="booking",
        entity_id=booking.id,
        metadata={"transaction_ref": transaction.transaction_ref, "reason": payload.reason},
    )
    db.commit()
    return {
        "message": "Refund recorded successfully",
        "booking_id": booking.id,
        "transaction_ref": transaction.transaction_ref,
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
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "payment_intent.succeeded":
        payment_intent = event["data"]["object"]
        booking_id = int(payment_intent.get("metadata", {}).get("booking_id", 0))
        if booking_id:
            booking = get_booking_or_404(db, booking_id)
            last4, brand = get_card_details(payment_intent)
            upsert_success_transaction(
                db=db,
                booking=booking,
                transaction_ref=payment_intent.get("metadata", {}).get(
                    "transaction_ref", "TXN-" + payment_intent["id"][-12:].upper()
                ),
                payment_method="card",
                payment_intent_id=payment_intent["id"],
                card_last4=last4,
                card_brand=brand,
            )

    if event["type"] == "payment_intent.payment_failed":
        payment_intent = event["data"]["object"]
        booking_id = int(payment_intent.get("metadata", {}).get("booking_id", 0))
        reason = (
            payment_intent.get("last_payment_error", {}) or {}
        ).get("message", "Payment declined")
        if booking_id:
            booking = get_booking_or_404(db, booking_id)
            if booking.payment_status != models.PaymentStatus.PAID:
                record_failed_transaction(
                    db=db,
                    booking=booking,
                    reason=reason,
                    payment_intent_id=payment_intent["id"],
                    transaction_ref=payment_intent.get("metadata", {}).get("transaction_ref"),
                )

    return {"status": "ok"}
