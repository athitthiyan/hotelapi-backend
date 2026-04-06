from __future__ import annotations

from sqlalchemy.orm import Session

import models
from services.inventory_service import confirm_inventory_for_booking


def reconcile_booking_payment_state(db: Session, booking: models.Booking | None) -> bool:
    if not booking:
        return False

    success_transaction = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking.id,
            models.Transaction.status == models.TransactionStatus.SUCCESS,
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )
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
        changed = reconcile_booking_payment_state(db, booking) or changed
    return changed
