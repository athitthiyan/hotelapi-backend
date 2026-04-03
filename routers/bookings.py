import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db

router = APIRouter(prefix="/bookings", tags=["Bookings"])

BOOKING_HOLD_MINUTES = 15


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_comparison_datetime(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def generate_booking_ref() -> str:
    return "BK" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def calculate_booking_amount(room: models.Room, nights: int):
    room_total = room.price * nights
    taxes = round(room_total * 0.12, 2)
    service_fee = round(room_total * 0.05, 2)
    total = round(room_total + taxes + service_fee, 2)
    return room_total, taxes, service_fee, total


def expire_stale_booking_hold(booking: models.Booking, now: Optional[datetime] = None) -> bool:
    now = now or utc_now()
    hold_expires_at = booking.hold_expires_at
    if hold_expires_at is not None:
        hold_expires_at = normalize_comparison_datetime(hold_expires_at, now)
    if (
        booking.status == models.BookingStatus.PENDING
        and booking.payment_status != models.PaymentStatus.PAID
        and hold_expires_at
        and hold_expires_at <= now
    ):
        booking.status = models.BookingStatus.EXPIRED
        booking.payment_status = models.PaymentStatus.EXPIRED
        return True
    return False


def release_expired_holds(
    db: Session,
    room_id: Optional[int] = None,
    booking_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    now = now or utc_now()
    query = db.query(models.Booking).filter(
        models.Booking.status == models.BookingStatus.PENDING,
        models.Booking.payment_status != models.PaymentStatus.PAID,
        models.Booking.hold_expires_at.is_not(None),
        models.Booking.hold_expires_at <= now,
    )
    if room_id is not None:
        query = query.filter(models.Booking.room_id == room_id)
    if booking_id is not None:
        query = query.filter(models.Booking.id == booking_id)

    expired_bookings = query.all()
    for booking in expired_bookings:
        booking.status = models.BookingStatus.EXPIRED
        booking.payment_status = models.PaymentStatus.EXPIRED

    if expired_bookings:
        db.commit()
    return len(expired_bookings)


def has_active_booking_overlap(
    db: Session,
    room_id: int,
    check_in: datetime,
    check_out: datetime,
    exclude_booking_id: Optional[int] = None,
) -> bool:
    now = utc_now()
    overlap_filter = and_(
        models.Booking.check_in < check_out,
        models.Booking.check_out > check_in,
    )
    active_booking_filter = or_(
        models.Booking.status == models.BookingStatus.CONFIRMED,
        and_(
            models.Booking.status == models.BookingStatus.PENDING,
            or_(
                models.Booking.hold_expires_at.is_(None),
                models.Booking.hold_expires_at > now,
            ),
        ),
    )

    query = db.query(models.Booking).filter(
        models.Booking.room_id == room_id,
        overlap_filter,
        active_booking_filter,
    )
    if exclude_booking_id is not None:
        query = query.filter(models.Booking.id != exclude_booking_id)

    return query.first() is not None


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


@router.post("", response_model=schemas.BookingResponse, status_code=201)
def create_booking(booking_data: schemas.BookingCreate, db: Session = Depends(get_db)):
    room = db.query(models.Room).filter(models.Room.id == booking_data.room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if not room.availability:
        raise HTTPException(status_code=400, detail="Room is not available")

    check_in = booking_data.check_in
    check_out = booking_data.check_out
    if check_out <= check_in:
        raise HTTPException(status_code=400, detail="Check-out must be after check-in")

    nights = (check_out - check_in).days
    if nights < 1:
        raise HTTPException(status_code=400, detail="Minimum stay is 1 night")

    release_expired_holds(db, room_id=booking_data.room_id)
    if has_active_booking_overlap(db, booking_data.room_id, check_in, check_out):
        raise HTTPException(
            status_code=409,
            detail="Room is already reserved for the selected dates",
        )

    room_rate, taxes, service_fee, total = calculate_booking_amount(room, nights)

    db_booking = models.Booking(
        booking_ref=generate_booking_ref(),
        user_name=booking_data.user_name,
        email=booking_data.email,
        phone=booking_data.phone,
        room_id=booking_data.room_id,
        check_in=check_in,
        check_out=check_out,
        hold_expires_at=utc_now() + timedelta(minutes=BOOKING_HOLD_MINUTES),
        guests=booking_data.guests,
        nights=nights,
        room_rate=room_rate,
        taxes=taxes,
        service_fee=service_fee,
        total_amount=total,
        special_requests=booking_data.special_requests,
        status=models.BookingStatus.PENDING,
        payment_status=models.PaymentStatus.PENDING,
    )
    db.add(db_booking)
    db.commit()
    db.refresh(db_booking)

    return get_booking_or_404(db, db_booking.id)


@router.get("", response_model=schemas.BookingListResponse)
def get_bookings(
    email: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(10),
    db: Session = Depends(get_db),
):
    release_expired_holds(db)
    query = db.query(models.Booking).options(joinedload(models.Booking.room))

    if email:
        query = query.filter(models.Booking.email == email)
    if status:
        query = query.filter(models.Booking.status == status)

    total = query.count()
    bookings = (
        query.order_by(models.Booking.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {"bookings": bookings, "total": total}


@router.get("/history", response_model=schemas.BookingListResponse)
def get_booking_history(
    email: str = Query(...),
    db: Session = Depends(get_db),
):
    release_expired_holds(db)
    bookings = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.email == email)
        .order_by(models.Booking.created_at.desc())
        .all()
    )

    return {"bookings": bookings, "total": len(bookings)}


@router.get("/{booking_id}", response_model=schemas.BookingResponse)
def get_booking(booking_id: int, db: Session = Depends(get_db)):
    release_expired_holds(db, booking_id=booking_id)
    return get_booking_or_404(db, booking_id)


@router.get("/ref/{booking_ref}", response_model=schemas.BookingResponse)
def get_booking_by_ref(booking_ref: str, db: Session = Depends(get_db)):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.booking_ref == booking_ref)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if expire_stale_booking_hold(booking):
        db.commit()
        db.refresh(booking)
    return booking


@router.patch("/{booking_id}/cancel", response_model=schemas.BookingResponse)
def cancel_booking(booking_id: int, db: Session = Depends(get_db)):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if expire_stale_booking_hold(booking):
        db.commit()
        db.refresh(booking)
        raise HTTPException(status_code=400, detail="Booking already expired")

    if booking.status == models.BookingStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Booking already cancelled")
    if booking.status == models.BookingStatus.EXPIRED:
        raise HTTPException(status_code=400, detail="Booking already expired")
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(
            status_code=400,
            detail="Paid bookings must use the refund or support workflow",
        )

    booking.status = models.BookingStatus.CANCELLED
    db.commit()
    db.refresh(booking)
    return booking
