from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.orm import Session, joinedload
from typing import Optional
import stripe, random, string, uuid
import models, schemas
from database import get_db, settings

router = APIRouter(prefix="/payments", tags=["Payments"])


def generate_txn_ref() -> str:
    return "TXN-" + str(uuid.uuid4()).upper()[:12]


# ─── Create Payment Intent (Stripe or Mock) ───────────────────────────────────

@router.post("/create-payment-intent")
def create_payment_intent(
    payload: schemas.CreatePaymentIntent,
    db: Session = Depends(get_db),
):
    booking = db.query(models.Booking).filter(models.Booking.id == payload.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(status_code=400, detail="Booking already paid")

    if payload.payment_method == "mock":
        # Simulate mock payment - return a client secret placeholder
        return {
            "client_secret": f"mock_{generate_txn_ref()}",
            "payment_intent_id": f"pi_mock_{uuid.uuid4().hex[:16]}",
            "amount": booking.total_amount,
            "currency": "usd",
            "booking_ref": booking.booking_ref,
            "mode": "mock",
        }

    # Real Stripe flow
    stripe.api_key = settings.stripe_secret_key
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(booking.total_amount * 100),  # cents
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
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e.user_message))


# ─── Confirm Payment Success ──────────────────────────────────────────────────

@router.post("/payment-success", response_model=schemas.TransactionResponse)
def confirm_payment_success(
    payload: schemas.PaymentSuccess,
    db: Session = Depends(get_db),
):
    booking = db.query(models.Booking).filter(models.Booking.id == payload.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Create transaction record
    transaction = models.Transaction(
        booking_id=booking.id,
        transaction_ref=payload.transaction_ref,
        stripe_payment_intent_id=payload.payment_intent_id,
        amount=booking.total_amount,
        currency="USD",
        payment_method=payload.payment_method,
        card_last4=payload.card_last4,
        card_brand=payload.card_brand,
        status=models.TransactionStatus.SUCCESS,
    )
    db.add(transaction)

    # Update booking
    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED

    db.commit()
    db.refresh(transaction)

    transaction = db.query(models.Transaction).options(
        joinedload(models.Transaction.booking).joinedload(models.Booking.room)
    ).filter(models.Transaction.id == transaction.id).first()

    return transaction


# ─── Simulate Payment Failure ─────────────────────────────────────────────────

@router.post("/payment-failure")
def record_payment_failure(
    booking_id: int,
    reason: str = "Payment declined",
    db: Session = Depends(get_db),
):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    transaction = models.Transaction(
        booking_id=booking.id,
        transaction_ref=generate_txn_ref(),
        amount=booking.total_amount,
        currency="USD",
        payment_method="card",
        status=models.TransactionStatus.FAILED,
        failure_reason=reason,
    )
    db.add(transaction)
    booking.payment_status = models.PaymentStatus.FAILED
    db.commit()
    db.refresh(transaction)
    return {"message": "Payment failure recorded", "transaction_ref": transaction.transaction_ref}


# ─── Transactions ─────────────────────────────────────────────────────────────

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
    transactions = query.order_by(models.Transaction.created_at.desc())\
                        .offset((page - 1) * per_page).limit(per_page).all()
    return {"transactions": transactions, "total": total}


@router.get("/transactions/{txn_id}", response_model=schemas.TransactionResponse)
def get_transaction(txn_id: int, db: Session = Depends(get_db)):
    txn = db.query(models.Transaction).options(
        joinedload(models.Transaction.booking).joinedload(models.Booking.room)
    ).filter(models.Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn


# ─── Stripe Webhook ───────────────────────────────────────────────────────────

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
        pi = event["data"]["object"]
        booking_id = int(pi["metadata"].get("booking_id", 0))
        if booking_id:
            booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
            if booking:
                booking.payment_status = models.PaymentStatus.PAID
                booking.status = models.BookingStatus.CONFIRMED
                db.commit()

    return {"status": "ok"}
