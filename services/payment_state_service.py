from __future__ import annotations

import time
from typing import Optional

import stripe
from sqlalchemy.orm import Session

import models
from database import settings
from services.inventory_service import confirm_inventory_for_booking
from services.notification_service import (
    queue_booking_confirmation_email,
    queue_payment_receipt_email,
)


PROCESSING_TRANSACTION_STATUSES = {
    models.TransactionStatus.PENDING,
    models.TransactionStatus.PROCESSING,
}


def get_latest_transaction_for_booking(
    db: Session,
    booking_id: int,
) -> models.Transaction | None:
    return (
        db.query(models.Transaction)
        .filter(models.Transaction.booking_id == booking_id)
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )


def get_success_transaction_for_booking(
    db: Session,
    booking_id: int,
) -> models.Transaction | None:
    return (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking_id,
            models.Transaction.status == models.TransactionStatus.SUCCESS,
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )


def _notification_exists(
    db: Session,
    *,
    booking_id: int,
    transaction_id: int,
    event_type: str,
) -> bool:
    return (
        db.query(models.NotificationOutbox.id)
        .filter(
            models.NotificationOutbox.booking_id == booking_id,
            models.NotificationOutbox.transaction_id == transaction_id,
            models.NotificationOutbox.event_type == event_type,
        )
        .first()
        is not None
    )


def _get_card_details(payment_intent: object) -> tuple[Optional[str], Optional[str]]:
    if isinstance(payment_intent, dict):
        charges = payment_intent.get("charges", {}).get("data", [])
    else:
        charges = getattr(payment_intent, "charges", {}).get("data", [])
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

        if payment_intent:
            status = (
                payment_intent.get("status")
                if isinstance(payment_intent, dict)
                else getattr(payment_intent, "status", None)
            )
            if status == "succeeded":
                last4, brand = _get_card_details(payment_intent)
                return True, last4, brand

        if attempt < safe_attempts - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

    return False, None, None


def reconcile_gateway_payment_state(
    db: Session,
    booking: models.Booking | None,
    *,
    attempts: int = 1,
    delay_seconds: float = 0.0,
) -> bool:
    if not booking:
        return False

    latest_transaction = get_latest_transaction_for_booking(db, booking.id)
    if (
        not latest_transaction
        or latest_transaction.payment_method != "card"
        or latest_transaction.status not in PROCESSING_TRANSACTION_STATUSES
        or not latest_transaction.stripe_payment_intent_id
    ):
        return False

    confirmed, last4, brand = verify_card_payment_intent_succeeded(
        latest_transaction.stripe_payment_intent_id,
        attempts=attempts,
        delay_seconds=delay_seconds,
    )
    if not confirmed:
        return False

    latest_transaction.status = models.TransactionStatus.SUCCESS
    latest_transaction.failure_reason = None
    latest_transaction.card_last4 = last4 or latest_transaction.card_last4
    latest_transaction.card_brand = brand or latest_transaction.card_brand

    changed = False
    if booking.payment_status != models.PaymentStatus.PAID:
        booking.payment_status = models.PaymentStatus.PAID
        changed = True
    if booking.status != models.BookingStatus.CONFIRMED:
        booking.status = models.BookingStatus.CONFIRMED
        changed = True

    confirm_inventory_for_booking(db, booking=booking)
    if not _notification_exists(
        db,
        booking_id=booking.id,
        transaction_id=latest_transaction.id,
        event_type="booking_confirmed",
    ):
        queue_booking_confirmation_email(db, booking, latest_transaction)
    if not _notification_exists(
        db,
        booking_id=booking.id,
        transaction_id=latest_transaction.id,
        event_type="payment_receipt",
    ):
        queue_payment_receipt_email(db, booking, latest_transaction)

    return changed or latest_transaction.status == models.TransactionStatus.SUCCESS


def derive_booking_lifecycle_state(
    booking: models.Booking | None,
    latest_transaction: models.Transaction | None = None,
) -> str | None:
    if not booking:
        return None

    lifecycle_state = "HOLD_CREATED"
    if booking.status == models.BookingStatus.CANCELLED:
        lifecycle_state = "CANCELLED"
    elif (
        booking.status == models.BookingStatus.EXPIRED
        or booking.payment_status == models.PaymentStatus.EXPIRED
    ):
        lifecycle_state = "EXPIRED"
    elif (
        booking.status == models.BookingStatus.CONFIRMED
        and booking.payment_status in {models.PaymentStatus.PAID, models.PaymentStatus.REFUNDED}
    ):
        lifecycle_state = "CONFIRMED"
    elif latest_transaction is not None:
        if latest_transaction.status == models.TransactionStatus.SUCCESS:
            lifecycle_state = "PAYMENT_SUCCESS"
        elif latest_transaction.status in {
            models.TransactionStatus.FAILED,
            models.TransactionStatus.EXPIRED,
        } or booking.payment_status == models.PaymentStatus.FAILED:
            lifecycle_state = "PAYMENT_FAILED"
        elif (
            latest_transaction.retry_of_transaction_id
            and latest_transaction.status in PROCESSING_TRANSACTION_STATUSES
        ):
            lifecycle_state = "PAYMENT_RETRY"
        elif latest_transaction.status in PROCESSING_TRANSACTION_STATUSES:
            lifecycle_state = "PAYMENT_PENDING"
    elif booking.status == models.BookingStatus.PROCESSING or booking.payment_status == models.PaymentStatus.PROCESSING:
        lifecycle_state = "PAYMENT_PENDING"

    return lifecycle_state


def attach_booking_lifecycle_state(
    db: Session,
    booking: models.Booking | None,
    latest_transaction: models.Transaction | None = None,
) -> str | None:
    if not booking:
        return None
    resolved_latest = latest_transaction or get_latest_transaction_for_booking(db, booking.id)
    lifecycle_state = derive_booking_lifecycle_state(booking, resolved_latest)
    setattr(booking, "lifecycle_state", lifecycle_state)
    return lifecycle_state


def attach_bookings_lifecycle_state(db: Session, bookings: list[models.Booking]) -> None:
    for booking in bookings:
        attach_booking_lifecycle_state(db, booking)


def attach_transaction_lifecycle_state(
    db: Session,
    transaction: models.Transaction | None,
) -> str | None:
    if not transaction:
        return None
    lifecycle_state = attach_booking_lifecycle_state(
        db,
        transaction.booking,
        latest_transaction=transaction,
    )
    setattr(transaction, "lifecycle_state", lifecycle_state)
    return lifecycle_state


def reconcile_booking_payment_state(db: Session, booking: models.Booking | None) -> bool:
    if not booking:
        return False

    success_transaction = get_success_transaction_for_booking(db, booking.id)
    if not success_transaction:
        return False

    changed = False
    if booking.payment_status != models.PaymentStatus.PAID:
        booking.payment_status = models.PaymentStatus.PAID
        changed = True
    if booking.status != models.BookingStatus.CONFIRMED:
        booking.status = models.BookingStatus.CONFIRMED
        changed = True

    if changed:
        confirm_inventory_for_booking(db, booking=booking)

    return changed


def reconcile_bookings_payment_states(db: Session, bookings: list[models.Booking]) -> bool:
    changed = False
    for booking in bookings:
        changed = reconcile_gateway_payment_state(db, booking) or changed
    return changed
