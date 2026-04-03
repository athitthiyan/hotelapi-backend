import uuid
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db, settings
from routers.bookings import expire_stale_booking_hold, release_expired_holds

router = APIRouter(prefix="/payments", tags=["Payments"])


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
) -> Optional[models.Transaction]:
    filters = [models.Transaction.booking_id == booking_id]
    reference_filters = []
    if transaction_ref:
        reference_filters.append(models.Transaction.transaction_ref == transaction_ref)
    if payment_intent_id:
        reference_filters.append(
            models.Transaction.stripe_payment_intent_id == payment_intent_id
        )
    if not reference_filters:
        return None
    return db.query(models.Transaction).filter(*filters, or_(*reference_filters)).first()


def apply_paid_state(booking: models.Booking) -> None:
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED


def apply_failed_state(booking: models.Booking) -> None:
    if booking.payment_status != models.PaymentStatus.PAID:
        booking.payment_status = models.PaymentStatus.FAILED


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

    db.commit()
    db.refresh(transaction)
    return get_transaction_with_booking(db, transaction.id)


def record_failed_transaction(
    db: Session,
    booking: models.Booking,
    reason: str,
    payment_method: str = "card",
    payment_intent_id: Optional[str] = None,
) -> models.Transaction:
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(status_code=409, detail="Booking is already paid")

    transaction = get_transaction_by_reference(
        db,
        booking_id=booking.id,
        payment_intent_id=payment_intent_id,
    )
    if not transaction:
        transaction = models.Transaction(
            booking_id=booking.id,
            transaction_ref=generate_txn_ref(),
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

    db.commit()
    db.refresh(transaction)
    return transaction


def get_card_details(payment_intent: dict) -> tuple[Optional[str], Optional[str]]:
    charges = payment_intent.get("charges", {}).get("data", [])
    if not charges:
        return None, None
    card = charges[0].get("payment_method_details", {}).get("card", {})
    return card.get("last4"), card.get("brand")


@router.post("/create-payment-intent")
def create_payment_intent(
    payload: schemas.CreatePaymentIntent,
    db: Session = Depends(get_db),
):
    booking = get_booking_or_404(db, payload.booking_id)
    release_expired_holds(db, booking_id=booking.id)
    db.refresh(booking)
    if expire_stale_booking_hold(booking):
        db.commit()
        db.refresh(booking)
    if booking.status == models.BookingStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Cancelled or expired bookings cannot be paid")
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(status_code=409, detail="Booking already paid")
    if get_success_transaction_for_booking(db, booking.id):
        raise HTTPException(status_code=409, detail="Booking already paid")

    if payload.payment_method == "mock":
        return {
            "client_secret": f"mock_{generate_txn_ref()}",
            "payment_intent_id": f"pi_mock_{uuid.uuid4().hex[:16]}",
            "amount": booking.total_amount,
            "currency": "usd",
            "booking_ref": booking.booking_ref,
            "mode": "mock",
        }

    stripe.api_key = settings.stripe_secret_key
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(booking.total_amount * 100),
            currency="usd",
            metadata={
                "booking_id": str(booking.id),
                "booking_ref": booking.booking_ref,
                "email": booking.email,
            },
            receipt_email=booking.email,
        )
        return {
            "client_secret": intent.client_secret,
            "payment_intent_id": intent.id,
            "amount": booking.total_amount,
            "currency": "usd",
            "booking_ref": booking.booking_ref,
            "mode": "stripe",
        }
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message))


@router.post("/payment-success", response_model=schemas.TransactionResponse)
def confirm_payment_success(
    payload: schemas.PaymentSuccess,
    db: Session = Depends(get_db),
):
    booking = get_booking_or_404(db, payload.booking_id)
    release_expired_holds(db, booking_id=booking.id)
    db.refresh(booking)
    if expire_stale_booking_hold(booking):
        db.commit()
        db.refresh(booking)
    if booking.status == models.BookingStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Cancelled or expired bookings cannot be paid")

    transaction = upsert_success_transaction(
        db=db,
        booking=booking,
        transaction_ref=payload.transaction_ref,
        payment_method=payload.payment_method,
        payment_intent_id=payload.payment_intent_id,
        card_last4=payload.card_last4,
        card_brand=payload.card_brand,
    )
    return transaction


@router.post("/payment-failure")
def record_payment_failure(
    booking_id: int,
    reason: str = "Payment declined",
    payment_intent_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    booking = get_booking_or_404(db, booking_id)
    release_expired_holds(db, booking_id=booking.id)
    db.refresh(booking)
    if expire_stale_booking_hold(booking):
        db.commit()
        db.refresh(booking)
        raise HTTPException(status_code=400, detail="Cancelled or expired bookings cannot be updated")
    transaction = record_failed_transaction(
        db=db,
        booking=booking,
        reason=reason,
        payment_intent_id=payment_intent_id,
    )
    return {"message": "Payment failure recorded", "transaction_ref": transaction.transaction_ref}


@router.get("/status/{booking_id}", response_model=schemas.PaymentStateResponse)
def get_payment_status(booking_id: int, db: Session = Depends(get_db)):
    booking = get_booking_or_404(db, booking_id)
    if expire_stale_booking_hold(booking):
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


@router.get("/transactions", response_model=schemas.TransactionListResponse)
def get_transactions(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(10),
    db: Session = Depends(get_db),
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
                transaction_ref="TXN-" + payment_intent["id"][-12:].upper(),
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
                )

    return {"status": "ok"}
