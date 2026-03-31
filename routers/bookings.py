from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from typing import Optional
from datetime import datetime, timedelta
import random, string
import models, schemas
from database import get_db

router = APIRouter(prefix="/bookings", tags=["Bookings"])


def generate_booking_ref() -> str:
    return "BK" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def calculate_booking_amount(room: models.Room, nights: int):
    room_total = room.price * nights
    taxes = round(room_total * 0.12, 2)       # 12% tax
    service_fee = round(room_total * 0.05, 2) # 5% service fee
    total = round(room_total + taxes + service_fee, 2)
    return room_total, taxes, service_fee, total


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

    room_rate, taxes, service_fee, total = calculate_booking_amount(room, nights)

    db_booking = models.Booking(
        booking_ref=generate_booking_ref(),
        user_name=booking_data.user_name,
        email=booking_data.email,
        phone=booking_data.phone,
        room_id=booking_data.room_id,
        check_in=check_in,
        check_out=check_out,
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

    # Eager load room
    db_booking = db.query(models.Booking).options(
        joinedload(models.Booking.room)
    ).filter(models.Booking.id == db_booking.id).first()

    return db_booking


@router.get("", response_model=schemas.BookingListResponse)
def get_bookings(
    email: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(10),
    db: Session = Depends(get_db),
):
    query = db.query(models.Booking).options(joinedload(models.Booking.room))

    if email:
        query = query.filter(models.Booking.email == email)
    if status:
        query = query.filter(models.Booking.status == status)

    total = query.count()
    bookings = query.order_by(models.Booking.created_at.desc())\
                    .offset((page - 1) * per_page).limit(per_page).all()

    return {"bookings": bookings, "total": total}


@router.get("/history", response_model=schemas.BookingListResponse)
def get_booking_history(
    email: str = Query(...),
    db: Session = Depends(get_db),
):
    bookings = db.query(models.Booking).options(
        joinedload(models.Booking.room)
    ).filter(models.Booking.email == email)\
     .order_by(models.Booking.created_at.desc()).all()

    return {"bookings": bookings, "total": len(bookings)}


@router.get("/{booking_id}", response_model=schemas.BookingResponse)
def get_booking(booking_id: int, db: Session = Depends(get_db)):
    booking = db.query(models.Booking).options(
        joinedload(models.Booking.room)
    ).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@router.get("/ref/{booking_ref}", response_model=schemas.BookingResponse)
def get_booking_by_ref(booking_ref: str, db: Session = Depends(get_db)):
    booking = db.query(models.Booking).options(
        joinedload(models.Booking.room)
    ).filter(models.Booking.booking_ref == booking_ref).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@router.patch("/{booking_id}/cancel", response_model=schemas.BookingResponse)
def cancel_booking(booking_id: int, db: Session = Depends(get_db)):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status == models.BookingStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Booking already cancelled")
    booking.status = models.BookingStatus.CANCELLED
    db.commit()
    db.refresh(booking)
    return booking
