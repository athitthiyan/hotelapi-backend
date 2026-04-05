import json
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db
from routers.auth import (
    build_token_response,
    get_current_partner,
    hash_password,
    verify_password,
)
from services.audit_service import write_audit_log
from services.inventory_service import calculate_effective_price, derive_inventory_status, get_or_create_inventory_row
from services.rate_limit_service import enforce_rate_limit

router = APIRouter(prefix="/partner", tags=["Partner"])

DEFAULT_COMMISSION_RATE = 0.15


def _mask_account_number(account_number: str | None) -> str | None:
    if not account_number:
        return None
    if len(account_number) <= 4:
        return account_number
    return f"{'*' * max(0, len(account_number) - 4)}{account_number[-4:]}"


def _decode_string_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _encode_string_list(values: list[str]) -> str:
    normalized = [value.strip() for value in values if value.strip()]
    return json.dumps(normalized)


def _get_partner_hotel_or_404(db: Session, partner_user_id: int) -> models.PartnerHotel:
    hotel = (
        db.query(models.PartnerHotel)
        .filter(models.PartnerHotel.owner_user_id == partner_user_id)
        .first()
    )
    if not hotel:
        raise HTTPException(status_code=404, detail="Partner hotel not found")
    return hotel


def _get_partner_room_or_404(
    db: Session,
    partner_hotel_id: int,
    room_id: int,
) -> models.Room:
    room = (
        db.query(models.Room)
        .filter(
            models.Room.id == room_id,
            models.Room.partner_hotel_id == partner_hotel_id,
        )
        .first()
    )
    if not room:
        raise HTTPException(status_code=404, detail="Partner room not found")
    return room


def _count_confirmed_bookings_for_date(db: Session, room_id: int, inventory_date: date) -> int:
    return (
        db.query(models.Booking)
        .filter(
            models.Booking.room_id == room_id,
            models.Booking.status.in_(
                [models.BookingStatus.CONFIRMED, models.BookingStatus.COMPLETED]
            ),
            models.Booking.check_in < datetime.combine(inventory_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
            models.Booking.check_out > datetime.combine(inventory_date, datetime.min.time(), tzinfo=timezone.utc),
        )
        .count()
    )


def _sync_inventory_row_counts(db: Session, row: models.RoomInventory) -> models.RoomInventory:
    row.booked_units = max(
        _count_confirmed_bookings_for_date(db, row.room_id, row.inventory_date),
        row.booked_units,
        row.total_units - row.available_units - row.locked_units - row.blocked_units,
        0,
    )
    row.status = derive_inventory_status(row)
    return row


def _get_or_create_partner_inventory_row(
    db: Session,
    *,
    room: models.Room,
    inventory_date: date,
) -> models.RoomInventory:
    row = get_or_create_inventory_row(
        db,
        room_id=room.id,
        inventory_date=inventory_date,
        default_total_units=room.total_room_count,
    )
    return _sync_inventory_row_counts(db, row)


def _validate_inventory_limits(
    *,
    total_units: int,
    available_units: int,
    blocked_units: int,
    booked_units: int,
    locked_units: int,
) -> None:
    if available_units < 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INVENTORY_CONFLICT",
                "message": "Available room count cannot be negative",
            },
        )
    if available_units > total_units:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INVENTORY_CONFLICT",
                "message": "Available room count cannot exceed total room count",
            },
        )
    if blocked_units < 0 or blocked_units > total_units:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INVENTORY_CONFLICT",
                "message": "Blocked room count is invalid for the selected dates",
            },
        )
    if booked_units + locked_units + blocked_units > total_units:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INVENTORY_CONFLICT",
                "message": "Cannot reduce rooms below confirmed bookings and active holds",
            },
        )
    if available_units + booked_units + locked_units + blocked_units > total_units:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INVENTORY_CONFLICT",
                "message": "Available count exceeds sellable inventory for the selected dates",
            },
        )


def _calendar_day_response(
    room: models.Room,
    row: models.RoomInventory,
) -> schemas.PartnerCalendarDay:
    return schemas.PartnerCalendarDay(
        date=row.inventory_date.isoformat(),
        total_units=row.total_units,
        available_units=row.available_units,
        locked_units=row.locked_units,
        booked_units=row.booked_units,
        blocked_units=row.blocked_units,
        effective_price=calculate_effective_price(room, inventory_date=row.inventory_date, inventory_row=row),
        block_reason=row.block_reason,
        price_override=row.price_override,
        price_override_label=row.price_override_label,
        status=row.status,
    )


def _serialize_partner_room(room: models.Room) -> schemas.PartnerRoomResponse:
    return schemas.PartnerRoomResponse(
        id=room.id,
        partner_hotel_id=room.partner_hotel_id,
        hotel_name=room.hotel_name,
        room_type=room.room_type,
        room_type_name=room.room_type_name,
        description=room.description,
        price=room.price,
        original_price=room.original_price,
        total_room_count=room.total_room_count,
        weekend_price=room.weekend_price,
        holiday_price=room.holiday_price,
        extra_guest_charge=room.extra_guest_charge,
        availability=room.availability,
        is_active=room.is_active,
        image_url=room.image_url,
        gallery_urls=_decode_string_list(room.gallery_urls),
        amenities=_decode_string_list(room.amenities),
        location=room.location,
        city=room.city,
        country=room.country,
        max_guests=room.max_guests,
        beds=room.beds,
        bathrooms=room.bathrooms,
        size_sqft=room.size_sqft,
        floor=room.floor,
        created_at=room.created_at,
    )


def _hotel_response(hotel: models.PartnerHotel) -> schemas.PartnerHotelResponse:
    return schemas.PartnerHotelResponse.model_validate(hotel)


@router.post("/register", response_model=schemas.TokenResponse, status_code=201)
def partner_register(
    payload: schemas.PartnerRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit("partner:register", request, subject=payload.email.lower())
    existing_user = db.query(models.User).filter(models.User.email == payload.email.lower()).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = models.User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        is_admin=False,
        is_partner=True,
        is_active=True,
    )
    db.add(user)
    db.flush()

    hotel = models.PartnerHotel(
        owner_user_id=user.id,
        legal_name=payload.legal_name,
        display_name=payload.display_name,
        gst_number=payload.gst_number,
        support_email=payload.support_email.lower(),
        support_phone=payload.support_phone,
        address_line=payload.address_line,
        city=payload.city,
        state=payload.state,
        country=payload.country,
        postal_code=payload.postal_code,
        bank_account_name=payload.bank_account_name,
        bank_account_number_masked=_mask_account_number(payload.bank_account_number),
        bank_ifsc=payload.bank_ifsc,
        bank_upi_id=payload.bank_upi_id,
    )
    db.add(hotel)
    db.commit()
    db.refresh(user)
    return build_token_response(user)


@router.post("/login", response_model=schemas.TokenResponse)
def partner_login(
    payload: schemas.PartnerLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit("partner:login", request, subject=payload.email.lower())
    user = db.query(models.User).filter(models.User.email == payload.email.lower()).first()
    if not user or not user.hashed_password or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")
    if not user.is_partner:
        raise HTTPException(status_code=403, detail="Partner access required")
    return build_token_response(user)


@router.get("/hotel", response_model=schemas.PartnerHotelResponse)
def get_partner_hotel(
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    return _hotel_response(_get_partner_hotel_or_404(db, partner_user.id))


@router.put("/hotel", response_model=schemas.PartnerHotelResponse)
def update_partner_hotel(
    payload: schemas.PartnerHotelUpdate,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    update_data = payload.model_dump(exclude_unset=True)
    bank_account_number = update_data.pop("bank_account_number", None)
    for field_name, value in update_data.items():
        setattr(hotel, field_name, value)
    if bank_account_number is not None:
        hotel.bank_account_number_masked = _mask_account_number(bank_account_number)

    db.commit()
    db.refresh(hotel)
    write_audit_log(
        db,
        actor_user_id=partner_user.id,
        action="partner.hotel.updated",
        entity_type="partner_hotel",
        entity_id=str(hotel.id),
        metadata={"fields": sorted(update_data.keys())},
    )
    return _hotel_response(hotel)


@router.get("/rooms", response_model=schemas.PartnerRoomListResponse)
def list_partner_rooms(
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    rooms = (
        db.query(models.Room)
        .filter(
            models.Room.partner_hotel_id == hotel.id,
            models.Room.deleted_at.is_(None),
        )
        .order_by(models.Room.created_at.desc(), models.Room.id.desc())
        .all()
    )
    return schemas.PartnerRoomListResponse(
        rooms=[_serialize_partner_room(room) for room in rooms],
        total=len(rooms),
    )


@router.get("/room-types", response_model=schemas.PartnerRoomListResponse)
def list_partner_room_types(
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    return list_partner_rooms(partner_user=partner_user, db=db)


@router.post("/rooms", response_model=schemas.PartnerRoomResponse, status_code=201)
def create_partner_room(
    payload: schemas.PartnerRoomCreate,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = models.Room(
        partner_hotel_id=hotel.id,
        hotel_name=hotel.display_name,
        room_type=payload.room_type,
        room_type_name=payload.room_type_name,
        description=payload.description,
        price=payload.price,
        original_price=payload.original_price,
        total_room_count=payload.total_room_count,
        weekend_price=payload.weekend_price,
        holiday_price=payload.holiday_price,
        extra_guest_charge=payload.extra_guest_charge,
        availability=payload.availability,
        is_active=payload.is_active,
        image_url=payload.image_url,
        gallery_urls=_encode_string_list(payload.gallery_urls),
        amenities=_encode_string_list(payload.amenities),
        location=payload.location or hotel.address_line,
        city=payload.city or hotel.city,
        country=payload.country or hotel.country,
        max_guests=payload.max_guests,
        beds=payload.beds,
        bathrooms=payload.bathrooms,
        size_sqft=payload.size_sqft,
        floor=payload.floor,
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    write_audit_log(
        db,
        actor_user_id=partner_user.id,
        action="partner.room.created",
        entity_type="room",
        entity_id=str(room.id),
        metadata={"partner_hotel_id": hotel.id},
    )
    return _serialize_partner_room(room)


@router.post("/room-types", response_model=schemas.PartnerRoomResponse, status_code=201)
def create_partner_room_type(
    payload: schemas.PartnerRoomCreate,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    return create_partner_room(payload=payload, partner_user=partner_user, db=db)


@router.put("/rooms/{room_id}", response_model=schemas.PartnerRoomResponse)
def update_partner_room(
    room_id: int,
    payload: schemas.PartnerRoomUpdate,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = _get_partner_room_or_404(db, hotel.id, room_id)
    if room.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Partner room not found")
    update_data = payload.model_dump(exclude_unset=True)
    gallery_urls = update_data.pop("gallery_urls", None)
    amenities = update_data.pop("amenities", None)
    for field_name, value in update_data.items():
        setattr(room, field_name, value)
    if gallery_urls is not None:
        room.gallery_urls = _encode_string_list(gallery_urls)
    if amenities is not None:
        room.amenities = _encode_string_list(amenities)
    if room.total_room_count < 1:
        raise HTTPException(status_code=409, detail={"code": "INVENTORY_CONFLICT", "message": "Room type must have at least one room"})
    db.commit()
    db.refresh(room)
    write_audit_log(
        db,
        actor_user_id=partner_user.id,
        action="partner.room.updated",
        entity_type="room",
        entity_id=str(room.id),
        metadata={"fields": sorted(payload.model_dump(exclude_unset=True).keys())},
    )
    return _serialize_partner_room(room)


@router.put("/room-types/{room_id}", response_model=schemas.PartnerRoomResponse)
def update_partner_room_type(
    room_id: int,
    payload: schemas.PartnerRoomUpdate,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    return update_partner_room(room_id=room_id, payload=payload, partner_user=partner_user, db=db)


@router.delete("/rooms/{room_id}", status_code=204)
def delete_partner_room(
    room_id: int,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = _get_partner_room_or_404(db, hotel.id, room_id)
    if room.deleted_at is not None:
        return None
    has_active_bookings = (
        db.query(models.Booking)
        .filter(
            models.Booking.room_id == room.id,
            models.Booking.status.in_(
                [
                    models.BookingStatus.PENDING,
                    models.BookingStatus.PROCESSING,
                    models.BookingStatus.CONFIRMED,
                ]
            ),
        )
        .count()
        > 0
    )
    if has_active_bookings:
        raise HTTPException(
            status_code=409,
            detail="Room has active bookings and cannot be deleted",
        )
    room.deleted_at = datetime.now(timezone.utc)
    room.is_active = False
    room.availability = False
    db.commit()
    write_audit_log(
        db,
        actor_user_id=partner_user.id,
        action="partner.room.deleted",
        entity_type="room",
        entity_id=str(room_id),
        metadata={"partner_hotel_id": hotel.id},
    )
    return None


@router.delete("/room-types/{room_id}", status_code=204)
def delete_partner_room_type(
    room_id: int,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    return delete_partner_room(room_id=room_id, partner_user=partner_user, db=db)


@router.get("/bookings", response_model=schemas.PartnerBookingListResponse)
def list_partner_bookings(
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    bookings = (
        db.query(models.Booking)
        .join(models.Room, models.Room.id == models.Booking.room_id)
        .options(joinedload(models.Booking.room))
        .filter(models.Room.partner_hotel_id == hotel.id)
        .order_by(models.Booking.created_at.desc(), models.Booking.id.desc())
        .all()
    )
    return schemas.PartnerBookingListResponse(bookings=bookings, total=len(bookings))


@router.get("/revenue", response_model=schemas.PartnerRevenueSummary)
def get_partner_revenue(
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    bookings = (
        db.query(models.Booking)
        .join(models.Room, models.Room.id == models.Booking.room_id)
        .filter(models.Room.partner_hotel_id == hotel.id)
        .all()
    )
    gross_revenue = sum(
        booking.total_amount
        for booking in bookings
        if booking.status in (models.BookingStatus.CONFIRMED, models.BookingStatus.COMPLETED)
    )
    commission_amount = round(gross_revenue * DEFAULT_COMMISSION_RATE, 2)
    payouts = (
        db.query(models.PartnerPayout)
        .filter(models.PartnerPayout.hotel_id == hotel.id)
        .all()
    )
    pending_payouts = sum(payout.net_amount for payout in payouts if payout.status != "paid")
    paid_out = sum(payout.net_amount for payout in payouts if payout.status == "paid")
    return schemas.PartnerRevenueSummary(
        total_bookings=len(bookings),
        confirmed_bookings=sum(
            1 for booking in bookings if booking.status in (models.BookingStatus.CONFIRMED, models.BookingStatus.COMPLETED)
        ),
        cancelled_bookings=sum(1 for booking in bookings if booking.status == models.BookingStatus.CANCELLED),
        gross_revenue=round(gross_revenue, 2),
        commission_amount=commission_amount,
        net_revenue=round(gross_revenue - commission_amount, 2),
        pending_payouts=round(pending_payouts, 2),
        paid_out=round(paid_out, 2),
    )


@router.get("/payouts", response_model=schemas.PartnerPayoutListResponse)
def get_partner_payouts(
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    payouts = (
        db.query(models.PartnerPayout)
        .filter(models.PartnerPayout.hotel_id == hotel.id)
        .order_by(models.PartnerPayout.created_at.desc(), models.PartnerPayout.id.desc())
        .all()
    )
    if not payouts:
        bookings = (
            db.query(models.Booking)
            .join(models.Room, models.Room.id == models.Booking.room_id)
            .filter(
                models.Room.partner_hotel_id == hotel.id,
                models.Booking.status.in_(
                    [models.BookingStatus.CONFIRMED, models.BookingStatus.COMPLETED]
                ),
            )
            .all()
        )
        for booking in bookings:
            gross_amount = booking.total_amount
            commission_amount = round(gross_amount * DEFAULT_COMMISSION_RATE, 2)
            payout = models.PartnerPayout(
                hotel_id=hotel.id,
                booking_id=booking.id,
                gross_amount=gross_amount,
                commission_amount=commission_amount,
                net_amount=round(gross_amount - commission_amount, 2),
                currency="INR",
                status="pending",
                payout_reference=f"payout_{uuid.uuid4().hex[:12]}",
            )
            db.add(payout)
        db.commit()
        payouts = (
            db.query(models.PartnerPayout)
            .filter(models.PartnerPayout.hotel_id == hotel.id)
            .order_by(models.PartnerPayout.created_at.desc(), models.PartnerPayout.id.desc())
            .all()
        )
    return schemas.PartnerPayoutListResponse(payouts=payouts, total=len(payouts))


def _partner_calendar_response(
    *,
    db: Session,
    hotel: models.PartnerHotel,
    room: models.Room,
    start_date: date,
    end_date: date,
) -> schemas.PartnerCalendarResponse:
    days: list[schemas.PartnerCalendarDay] = []
    current_date = start_date
    while current_date <= end_date:
        row = _get_or_create_partner_inventory_row(db, room=room, inventory_date=current_date)
        days.append(_calendar_day_response(room, row))
        current_date += timedelta(days=1)
    db.commit()
    return schemas.PartnerCalendarResponse(room_id=room.id, hotel_id=hotel.id, days=days)


@router.get("/calendar", response_model=schemas.PartnerCalendarResponse)
def get_partner_calendar(
    room_type_id: int | None = Query(default=None, gt=0),
    room_id: int | None = Query(default=None, gt=0),
    start_date: date | None = Query(default=None),
    lookahead_days: int = Query(default=30, ge=1, le=180),
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    target_room_id = room_type_id or room_id
    if target_room_id is None:
        raise HTTPException(status_code=422, detail="room_type_id is required")
    room = _get_partner_room_or_404(db, hotel.id, target_room_id)
    start_date = start_date or date.today()
    end_date = start_date + timedelta(days=lookahead_days - 1)
    return _partner_calendar_response(
        db=db,
        hotel=hotel,
        room=room,
        start_date=start_date,
        end_date=end_date,
    )


def _apply_inventory_update(
    *,
    db: Session,
    room: models.Room,
    current_date: date,
    total_units: int | None,
    available_units: int | None,
    blocked_units: int | None,
    block_reason: str | None,
    status: models.InventoryStatus,
) -> None:
    row = _get_or_create_partner_inventory_row(db, room=room, inventory_date=current_date)
    target_total = total_units if total_units is not None else row.total_units
    target_blocked = blocked_units if blocked_units is not None else row.blocked_units
    max_available = target_total - row.locked_units - row.booked_units - target_blocked
    target_available = max_available if available_units is None else available_units
    _validate_inventory_limits(
        total_units=target_total,
        available_units=target_available,
        blocked_units=target_blocked,
        booked_units=row.booked_units,
        locked_units=row.locked_units,
    )
    row.total_units = target_total
    row.blocked_units = target_blocked
    row.available_units = target_available
    row.block_reason = block_reason
    row.status = status if target_available <= 0 else derive_inventory_status(row)


@router.put("/calendar", response_model=schemas.PartnerCalendarResponse)
def update_partner_calendar(
    payload: schemas.PartnerInventoryUpdateRequest,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = _get_partner_room_or_404(db, hotel.id, payload.room_type_id)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    current_date = payload.start_date
    while current_date <= payload.end_date:
        _apply_inventory_update(
            db=db,
            room=room,
            current_date=current_date,
            total_units=payload.total_units,
            available_units=payload.available_units,
            blocked_units=payload.blocked_units,
            block_reason=payload.block_reason,
            status=payload.status,
        )
        current_date += timedelta(days=1)
    db.commit()
    write_audit_log(
        db,
        actor_user_id=partner_user.id,
        action="partner.calendar.updated",
        entity_type="room_inventory",
        entity_id=str(room.id),
        metadata={
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "room_type_id": room.id,
        },
    )
    return _partner_calendar_response(
        db=db,
        hotel=hotel,
        room=room,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )


@router.post("/inventory/block", response_model=schemas.PartnerCalendarResponse)
def block_partner_inventory(
    payload: schemas.PartnerInventoryUpdateRequest,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = _get_partner_room_or_404(db, hotel.id, payload.room_type_id)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    current_date = payload.start_date
    while current_date <= payload.end_date:
        row = _get_or_create_partner_inventory_row(db, room=room, inventory_date=current_date)
        remaining_sellable = row.total_units - row.booked_units - row.locked_units - row.blocked_units
        requested_block = payload.blocked_units if payload.blocked_units is not None else remaining_sellable
        target_blocked = row.blocked_units + requested_block
        _apply_inventory_update(
            db=db,
            room=room,
            current_date=current_date,
            total_units=row.total_units,
            available_units=max(0, row.available_units - requested_block),
            blocked_units=target_blocked,
            block_reason=payload.block_reason or "maintenance",
            status=models.InventoryStatus.BLOCKED if remaining_sellable - requested_block <= 0 else models.InventoryStatus.AVAILABLE,
        )
        current_date += timedelta(days=1)
    db.commit()
    return _partner_calendar_response(
        db=db,
        hotel=hotel,
        room=room,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )


@router.post("/inventory/unblock", response_model=schemas.PartnerCalendarResponse)
def unblock_partner_inventory(
    payload: schemas.PartnerInventoryUpdateRequest,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = _get_partner_room_or_404(db, hotel.id, payload.room_type_id)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    current_date = payload.start_date
    while current_date <= payload.end_date:
        row = _get_or_create_partner_inventory_row(db, room=room, inventory_date=current_date)
        unblock_units = payload.blocked_units if payload.blocked_units is not None else row.blocked_units
        new_blocked = max(0, row.blocked_units - unblock_units)
        restored_available = min(
            row.total_units - row.booked_units - row.locked_units - new_blocked,
            row.available_units + unblock_units,
        )
        _apply_inventory_update(
            db=db,
            room=room,
            current_date=current_date,
            total_units=row.total_units,
            available_units=max(0, restored_available),
            blocked_units=new_blocked,
            block_reason=None if new_blocked == 0 else row.block_reason,
            status=models.InventoryStatus.AVAILABLE,
        )
        current_date += timedelta(days=1)
    db.commit()
    return _partner_calendar_response(
        db=db,
        hotel=hotel,
        room=room,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )


@router.post("/pricing", response_model=schemas.PartnerPricingCalendarResponse)
def create_partner_pricing_override(
    payload: schemas.PartnerPricingUpdateRequest,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    return update_partner_pricing(
        room_type_id=payload.room_type_id,
        payload=payload,
        partner_user=partner_user,
        db=db,
    )


@router.put("/pricing/{room_type_id}", response_model=schemas.PartnerPricingCalendarResponse)
def update_partner_pricing(
    room_type_id: int,
    payload: schemas.PartnerPricingUpdateRequest,
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = _get_partner_room_or_404(db, hotel.id, room_type_id)
    current_date = payload.start_date
    while current_date <= payload.end_date:
        row = _get_or_create_partner_inventory_row(db, room=room, inventory_date=current_date)
        row.price_override = payload.price
        row.price_override_label = payload.label
        current_date += timedelta(days=1)
    db.commit()
    return get_partner_pricing_calendar(
        room_type_id=room_type_id,
        start_date=payload.start_date,
        lookahead_days=(payload.end_date - payload.start_date).days + 1,
        partner_user=partner_user,
        db=db,
    )


@router.get("/pricing/calendar", response_model=schemas.PartnerPricingCalendarResponse)
def get_partner_pricing_calendar(
    room_type_id: int = Query(gt=0),
    start_date: date | None = Query(default=None),
    lookahead_days: int = Query(default=30, ge=1, le=180),
    partner_user: models.User = Depends(get_current_partner),
    db: Session = Depends(get_db),
):
    hotel = _get_partner_hotel_or_404(db, partner_user.id)
    room = _get_partner_room_or_404(db, hotel.id, room_type_id)
    start_date = start_date or date.today()
    days: list[schemas.PartnerPricingCalendarDay] = []
    for offset in range(lookahead_days):
        current_date = start_date + timedelta(days=offset)
        row = _get_or_create_partner_inventory_row(db, room=room, inventory_date=current_date)
        days.append(
            schemas.PartnerPricingCalendarDay(
                date=current_date.isoformat(),
                base_price=room.price,
                weekend_price=room.weekend_price,
                holiday_price=room.holiday_price,
                effective_price=calculate_effective_price(room, inventory_date=current_date, inventory_row=row),
                override_price=row.price_override,
                override_label=row.price_override_label,
            )
        )
    db.commit()
    return schemas.PartnerPricingCalendarResponse(room_type_id=room.id, hotel_id=hotel.id, days=days)
