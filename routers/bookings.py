import logging
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session, joinedload

import models
import schemas
import error_codes
from error_codes import booking_error
from database import get_db, settings
from routers.auth import (
    get_current_admin,
    get_current_user,
    get_optional_current_user,
    normalize_email,
)
from services.inventory_service import (
    calculate_effective_price,
    get_or_create_inventory_rows_for_stay,
    iter_stay_dates,
    lock_inventory_for_booking,
    release_expired_inventory_locks,
    release_inventory_for_booking,
)
from services.search_service import clear_search_cache
from services.notification_service import (
    queue_booking_cancellation_email,
    queue_booking_hold_email,
    queue_booking_support_request_email,
)
from services.payment_state_service import (
    attach_booking_lifecycle_state,
    attach_bookings_lifecycle_state,
    reconcile_booking_payment_state,
    reconcile_bookings_payment_states,
    reconcile_gateway_payment_state,
)
from services.audit_service import write_audit_log
from services.document_service import (
    build_invoice_pdf,
    build_voucher_pdf,
    invoice_number_for_booking,
)

router = APIRouter(prefix="/bookings", tags=["Bookings"])
logger = logging.getLogger(__name__)

BOOKING_HOLD_MINUTES = 10
VISIBLE_ACTIVE_HOLD_LIFECYCLE_STATES = {
    "HOLD_CREATED",
    "PAYMENT_FAILED",
    "PAYMENT_PENDING",
    "PAYMENT_COOLDOWN",
}


# Late-import broadcast_event to avoid circular imports
def _broadcast(event_type: str, payload: dict, source: str = "system"):
    """Fire-and-forget WebSocket broadcast for real-time sync."""
    try:
        from main import broadcast_event
        broadcast_event(event_type, payload, source)
    except Exception as exc:
        logger.exception("Failed to broadcast event %s: %s", event_type, exc)


def resolve_authenticated_booking_user(
    current_user: Optional[models.User],
) -> Optional[models.User]:
    if isinstance(current_user, models.User):
        return current_user
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Hotel business-day cutoff ────────────────────────────────────────────────
# The hotel's operational day extends past midnight until BUSINESS_DAY_CUTOFF_HOUR
# in the hotel's local timezone.  A guest booking for "today" remains valid until
# that cutoff hour of the *next* calendar day.
#
# Example (IST, cutoff=3):
#   Business date 2026-04-10 is valid from 2026-04-10 03:00 IST until 2026-04-11 03:00 IST.
#   At 01:30 AM IST on 2026-04-11, booking for 2026-04-10 is still allowed.
HOTEL_TZ = timezone(timedelta(hours=5, minutes=30))  # IST
BUSINESS_DAY_CUTOFF_HOUR = 3  # 3:00 AM local time


def get_business_date_start(now: datetime | None = None) -> datetime:
    """Return the start-of-day (midnight UTC) of the current *business* date.

    If local hotel time is before the cutoff hour (e.g. 3 AM), the business day
    is still "yesterday", so we return yesterday's midnight UTC.
    Otherwise we return today's midnight UTC.
    """
    if now is None:
        now = utc_now()
    local_now = now.astimezone(HOTEL_TZ)
    business_date = local_now.date()
    if local_now.hour < BUSINESS_DAY_CUTOFF_HOUR:
        business_date = business_date - timedelta(days=1)
    # Return midnight UTC of the business date for comparison with check_in timestamps
    return datetime(business_date.year, business_date.month, business_date.day, tzinfo=timezone.utc)



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


def should_return_active_hold(booking: models.Booking, remaining_seconds: int) -> bool:
    if remaining_seconds <= 0:
        return False

    if booking.status in {
        models.BookingStatus.CANCELLED,
        models.BookingStatus.CONFIRMED,
        models.BookingStatus.COMPLETED,
        models.BookingStatus.EXPIRED,
    }:
        return False

    if booking.payment_status in {
        models.PaymentStatus.PAID,
        models.PaymentStatus.REFUNDED,
        models.PaymentStatus.EXPIRED,
    }:
        return False

    lifecycle_state = getattr(booking, "lifecycle_state", None)
    if lifecycle_state:
        return lifecycle_state in VISIBLE_ACTIVE_HOLD_LIFECYCLE_STATES

    return True


def calculate_booking_amount_for_dates(
    db: Session,
    *,
    room: models.Room,
    check_in: datetime,
    check_out: datetime,
):
    inventory_rows = get_or_create_inventory_rows_for_stay(
        db,
        room_id=room.id,
        check_in=check_in,
        check_out=check_out,
        default_total_units=room.total_room_count,
    )
    rows_by_date = {row.inventory_date: row for row in inventory_rows}
    room_total = 0.0
    nights = 0
    for stay_date in iter_stay_dates(check_in, check_out):
        room_total += calculate_effective_price(
            room,
            inventory_date=stay_date,
            inventory_row=rows_by_date.get(stay_date),
        )
        nights += 1
    taxes = round(room_total * 0.12, 2)
    service_fee = round(room_total * 0.05, 2)
    total = round(room_total + taxes + service_fee, 2)
    return round(room_total, 2), taxes, service_fee, total, nights


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


def has_active_pending_hold(booking: models.Booking, now: Optional[datetime] = None) -> bool:
    now = now or utc_now()
    hold_expires_at = booking.hold_expires_at
    if hold_expires_at is not None:
        hold_expires_at = normalize_comparison_datetime(hold_expires_at, now)
    return (
        booking.status == models.BookingStatus.PENDING
        and booking.payment_status != models.PaymentStatus.PAID
        and hold_expires_at is not None
        and hold_expires_at > now
    )


def has_active_user_hold(booking: models.Booking, now: Optional[datetime] = None) -> bool:
    now = now or utc_now()
    hold_expires_at = booking.hold_expires_at
    if hold_expires_at is not None:
        hold_expires_at = normalize_comparison_datetime(hold_expires_at, now)
    return (
        booking.status in [models.BookingStatus.PENDING, models.BookingStatus.PROCESSING]
        and booking.payment_status not in [models.PaymentStatus.PAID, models.PaymentStatus.EXPIRED]
        and hold_expires_at is not None
        and hold_expires_at > now
    )


def get_latest_active_hold_for_user(
    db: Session,
    user: models.User,
    now: Optional[datetime] = None,
) -> Optional[models.Booking]:
    now = now or utc_now()
    candidate_bookings = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(
            or_(
                models.Booking.user_id == user.id,
                models.Booking.email == user.email,
            ),
            models.Booking.hold_expires_at.is_not(None),
        )
        .order_by(models.Booking.created_at.desc())
        .all()
    )
    if reconcile_bookings_payment_states(db, candidate_bookings):
        db.commit()
        for booking in candidate_bookings:
            db.refresh(booking)
    return next(
        (booking for booking in candidate_bookings if has_active_user_hold(booking, now=now)),
        None,
    )


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
    )
    if room_id is not None:
        query = query.filter(models.Booking.room_id == room_id)
    if booking_id is not None:
        query = query.filter(models.Booking.id == booking_id)

    candidate_bookings = query.all()
    expired_bookings = []
    for booking in candidate_bookings:
        if expire_stale_booking_hold(booking, now=now):
            release_inventory_for_booking(db, booking=booking)
            expired_bookings.append(booking)

    if expired_bookings:
        db.commit()
        clear_search_cache()
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

    query = db.query(models.Booking).options(
        joinedload(models.Booking.room)
    ).filter(
        models.Booking.room_id == room_id,
        overlap_filter,
        models.Booking.status.in_(
            [models.BookingStatus.CONFIRMED, models.BookingStatus.PENDING]
        ),
    )
    if exclude_booking_id is not None:
        query = query.filter(models.Booking.id != exclude_booking_id)

    for booking in query.all():
        if booking.status == models.BookingStatus.CONFIRMED:
            return True
        if has_active_pending_hold(booking, now=now):
            return True
    return False


def get_booking_or_404(db: Session, booking_id: int) -> models.Booking:
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise booking_error(
            status_code=404,
            code=error_codes.HOLD_NOT_FOUND,
            message="Booking not found",
        )
    return booking


@router.post("", response_model=schemas.BookingResponse, status_code=201)
def create_booking(
    booking_data: schemas.BookingCreate,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(get_optional_current_user),
):
    current_user = resolve_authenticated_booking_user(current_user)
    room = db.query(models.Room).filter(models.Room.id == booking_data.room_id).first()
    if not room:
        raise booking_error(
            status_code=404,
            code=error_codes.ROOM_NOT_FOUND,
            message="Room not found",
        )
    if not room.availability or not room.is_active or room.deleted_at is not None:
        raise booking_error(
            status_code=400,
            code=error_codes.ROOM_UNAVAILABLE,
            message="This room is not currently available for booking",
        )

    check_in = booking_data.check_in
    check_out = booking_data.check_out

    # SQLite returns naive datetimes; normalise to UTC-aware so all comparisons work
    if check_in.tzinfo is None:
        check_in = check_in.replace(tzinfo=timezone.utc)
    if check_out.tzinfo is None:
        check_out = check_out.replace(tzinfo=timezone.utc)

    if check_out <= check_in:
        raise booking_error(
            status_code=400,
            code=error_codes.INVALID_DATE_RANGE,
            message="Check-out date must be after check-in date",
            field="check_out",
        )

    nights = (check_out - check_in).days
    if nights < 1:
        raise booking_error(
            status_code=400,
            code=error_codes.MINIMUM_STAY,
            message="Minimum stay is 1 night",
            field="check_out",
        )

    # Check if check-in date is still valid under hotel business-day rules.
    # The hotel operational day extends until 3:00 AM local time, so same-day
    # bookings are allowed even after midnight until that cutoff.
    now = utc_now()
    business_day_start = get_business_date_start(now)
    if check_in < business_day_start:
        raise booking_error(
            status_code=400,
            code=error_codes.CHECK_IN_PAST,
            message="Check-in date must be in the future",
            field="check_in",
        )

    # Check if guest count exceeds room capacity
    # Use the larger of guests vs adults+children for backward compatibility
    effective_guests = max(booking_data.guests, booking_data.adults + booking_data.children)
    if effective_guests > room.max_guests:
        raise booking_error(
            status_code=400,
            code=error_codes.GUEST_CAPACITY_EXCEEDED,
            message=f"This room accommodates a maximum of {room.max_guests} guests",
            field="guests",
        )

    normalized_email = normalize_email(booking_data.email)
    linked_user = (
        db.query(models.User)
        .filter(models.User.email == normalized_email, models.User.is_active.is_(True))
        .first()
    )
    booking_owner = current_user or linked_user
    release_expired_holds(db, room_id=booking_data.room_id)
    release_expired_inventory_locks(db, room_id=booking_data.room_id)

    # Logged-in/known users may only have one active booking hold at a time.
    if booking_owner:
        existing_user_hold = get_latest_active_hold_for_user(db, booking_owner, now=now)
        if existing_user_hold:
            raise booking_error(
                status_code=409,
                code=error_codes.HOLD_EXISTS,
                message="You already have an active booking hold. Please complete or cancel your existing reservation first.",
                field="booking_id",
            )

    # Check for duplicate active hold (same room + overlapping dates + non-expired hold)
    existing_holds = (
        db.query(models.Booking)
        .filter(
            models.Booking.room_id == booking_data.room_id,
            models.Booking.check_in < check_out,
            models.Booking.check_out > check_in,
            models.Booking.status == models.BookingStatus.PENDING,
            models.Booking.payment_status != models.PaymentStatus.PAID,
            models.Booking.hold_expires_at.is_not(None),
        )
        .all()
    )
    if any(has_active_pending_hold(booking, now=now) for booking in existing_holds):
        raise booking_error(
            status_code=409,
            code=error_codes.HOLD_EXISTS,
            message="An active booking hold already exists for these dates. Please complete your existing reservation.",
            field="date_range",
        )

    if has_active_booking_overlap(db, booking_data.room_id, check_in, check_out):
        raise booking_error(
            status_code=409,
            code=error_codes.BOOKING_CONFLICT,
            message="These dates are no longer available. Please choose different dates.",
            field="date_range",
        )

    room_rate, taxes, service_fee, total, nights = calculate_booking_amount_for_dates(
        db,
        room=room,
        check_in=check_in,
        check_out=check_out,
    )
    db_booking = models.Booking(
        booking_ref=generate_booking_ref(),
        user_name=booking_data.user_name,
        email=normalized_email,
        user_id=booking_owner.id if booking_owner else None,
        phone=booking_data.phone,
        room_id=booking_data.room_id,
        check_in=check_in,
        check_out=check_out,
        hold_expires_at=utc_now() + timedelta(minutes=BOOKING_HOLD_MINUTES),
        guests=max(booking_data.guests, booking_data.adults + booking_data.children),
        adults=booking_data.adults,
        children=booking_data.children,
        infants=booking_data.infants,
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
    db.flush()
    try:
        lock_inventory_for_booking(
            db,
            booking=db_booking,
            lock_expires_at=db_booking.hold_expires_at,
        )
    except ValueError as exc:
        db.rollback()
        raise booking_error(
            status_code=409,
            code=error_codes.BOOKING_CONFLICT,
            message="These dates are no longer available. Please choose different dates.",
            field="date_range",
        ) from exc
    queue_booking_hold_email(db, db_booking)
    db.commit()
    db.refresh(db_booking)
    clear_search_cache()

    # ── Real-time broadcast: booking created ──
    _broadcast("booking-created", {
        "booking_id": db_booking.id,
        "booking_ref": db_booking.booking_ref,
        "room_id": db_booking.room_id,
        "status": db_booking.status.value if hasattr(db_booking.status, 'value') else str(db_booking.status),
        "total_amount": db_booking.total_amount,
    }, source="customer")

    _broadcast("inventory-updated", {
        "room_id": db_booking.room_id,
        "action": "lock",
        "booking_id": db_booking.id,
    }, source="system")

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
        query = query.filter(models.Booking.email == normalize_email(email))
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
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    release_expired_holds(db)
    normalized_email = normalize_email(email)
    user = db.query(models.User).filter(models.User.email == normalized_email).first()

    # Get total count
    total = (
        db.query(func.count(models.Booking.id))
        .filter(
            or_(
                models.Booking.email == normalized_email,
                models.Booking.user_id == (user.id if user else -1),
            )
        )
        .scalar()
        or 0
    )

    # Get paginated bookings
    bookings = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(
            or_(
                models.Booking.email == normalized_email,
                models.Booking.user_id == (user.id if user else -1),
            )
        )
        .order_by(models.Booking.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    if reconcile_bookings_payment_states(db, bookings):
        db.commit()
        for booking in bookings:
            db.refresh(booking)
    attach_bookings_lifecycle_state(db, bookings)

    return {"bookings": bookings, "total": total}


@router.get(
    "/active-hold",
    response_model=schemas.ActiveHoldResponse,
    responses={204: {"description": "No active booking hold"}},
)
def get_active_hold(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    release_expired_holds(db)
    active_hold = get_latest_active_hold_for_user(db, user)
    if not active_hold or not active_hold.room or not active_hold.hold_expires_at:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if reconcile_gateway_payment_state(
        db,
        active_hold,
        attempts=2,
        delay_seconds=0.25,
    ) or reconcile_booking_payment_state(db, active_hold):
        db.commit()
        db.refresh(active_hold)
        if (
            active_hold.payment_status == models.PaymentStatus.PAID
            or active_hold.status == models.BookingStatus.CONFIRMED
        ):
            return Response(status_code=status.HTTP_204_NO_CONTENT)

    lifecycle_state = attach_booking_lifecycle_state(db, active_hold)
    now = utc_now()
    normalized_expiry = normalize_comparison_datetime(active_hold.hold_expires_at, now)
    remaining_seconds = max(0, int((normalized_expiry - now).total_seconds()))
    if not should_return_active_hold(active_hold, remaining_seconds):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return {
        "booking_id": active_hold.id,
        "room_id": active_hold.room_id,
        "hotel_name": active_hold.room.hotel_name,
        "room_name": active_hold.room.room_type.value,
        "check_in": active_hold.check_in.date(),
        "check_out": active_hold.check_out.date(),
        "guests": active_hold.guests,
        "expires_at": active_hold.hold_expires_at,
        "remaining_seconds": remaining_seconds,
        "lifecycle_state": lifecycle_state,
        "booking_status": active_hold.status.value,
        "payment_status": active_hold.payment_status.value,
    }


@router.get("/resumable", response_model=schemas.BookingResponse)
def get_resumable_booking(
    room_id: int = Query(..., gt=0),
    check_in: datetime = Query(...),
    check_out: datetime = Query(...),
    email: str = Query(...),
    db: Session = Depends(get_db),
):
    """Return an existing PENDING booking for the same room/dates/email if its hold
    has not yet expired — allows the frontend to reuse the booking ID for retry payment."""
    release_expired_holds(db, room_id=room_id)
    now = utc_now()
    candidate_bookings = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(
            models.Booking.room_id == room_id,
            models.Booking.email == normalize_email(email),
            models.Booking.status == models.BookingStatus.PENDING,
            models.Booking.payment_status != models.PaymentStatus.PAID,
            models.Booking.hold_expires_at.is_not(None),
        )
        .order_by(models.Booking.created_at.desc())
        .all()
    )
    normalized_check_in = normalize_comparison_datetime(check_in, now)
    normalized_check_out = normalize_comparison_datetime(check_out, now)
    booking = next(
        (
            candidate
            for candidate in candidate_bookings
            if normalize_comparison_datetime(candidate.check_in, normalized_check_in) == normalized_check_in
            and normalize_comparison_datetime(candidate.check_out, normalized_check_out) == normalized_check_out
            and has_active_pending_hold(candidate, now=now)
        ),
        None,
    )
    if not booking:
        raise HTTPException(status_code=404, detail="No resumable booking found")
    return booking


def get_booking_with_room_or_404(db: Session, booking_id: int) -> models.Booking:
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.room).joinedload(models.Room.partner_hotel),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise booking_error(
            status_code=404,
            code=error_codes.HOLD_NOT_FOUND,
            message="Booking not found",
        )
    return booking


def can_access_booking_document(
    booking: models.Booking,
    current_user: Optional[models.User],
    booking_ref: Optional[str],
) -> bool:
    user = resolve_authenticated_booking_user(current_user)
    if user and (user.is_admin or booking.user_id == user.id or booking.email == user.email):
        return True
    return bool(booking_ref and booking_ref == booking.booking_ref)


def build_document_response(content: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([content]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ExtendHoldRequest(schemas.BaseModel):
    email: str


@router.post("/{booking_id}/extend-hold", response_model=schemas.BookingResponse)
def extend_booking_hold(
    booking_id: int,
    payload: ExtendHoldRequest,
    db: Session = Depends(get_db),
):
    """Re-lock inventory and extend the hold window for a booking whose hold has expired
    or is about to expire. The caller must supply the original booking email to prevent
    unauthorised extensions."""
    booking = get_booking_or_404(db, booking_id)

    # Email guard — prevents anyone with just a booking_id from extending a hold
    if booking.email.lower() != payload.email.strip().lower():
        raise booking_error(
            status_code=403,
            code=error_codes.AUTH_REQUIRED,
            message="Email does not match booking record",
        )

    if booking.payment_status == models.PaymentStatus.PAID:
        raise booking_error(
            status_code=409,
            code=error_codes.DUPLICATE_BOOKING,
            message="This booking has already been paid and confirmed",
        )
    if booking.status in [models.BookingStatus.CONFIRMED, models.BookingStatus.CANCELLED]:
        raise booking_error(
            status_code=409,
            code=error_codes.DUPLICATE_BOOKING,
            message="This booking has already been paid and confirmed",
        )

    # Release any stale locks for this room then recheck availability
    release_expired_holds(db, room_id=booking.room_id)
    release_expired_inventory_locks(db, room_id=booking.room_id)

    if has_active_booking_overlap(
        db, booking.room_id, booking.check_in, booking.check_out,
        exclude_booking_id=booking_id,
    ):
        raise booking_error(
            status_code=409,
            code=error_codes.BOOKING_CONFLICT,
            message="These dates are no longer available — another booking was confirmed",
        )

    new_expiry = utc_now() + timedelta(minutes=BOOKING_HOLD_MINUTES)

    try:
        lock_inventory_for_booking(db, booking=booking, lock_expires_at=new_expiry)
    except ValueError as exc:
        raise booking_error(
            status_code=409,
            code=error_codes.BOOKING_CONFLICT,
            message="These dates are no longer available — another booking was confirmed",
        ) from exc

    booking.hold_expires_at = new_expiry
    booking.status = models.BookingStatus.PENDING
    booking.payment_status = models.PaymentStatus.PENDING
    db.commit()
    db.refresh(booking)
    clear_search_cache()
    return get_booking_or_404(db, booking_id)


@router.get("/{booking_id}", response_model=schemas.BookingResponse)
def get_booking(booking_id: int, db: Session = Depends(get_db)):
    release_expired_holds(db, booking_id=booking_id)
    booking = get_booking_or_404(db, booking_id)
    if reconcile_gateway_payment_state(db, booking, attempts=2, delay_seconds=0.25):
        db.commit()
        db.refresh(booking)
    elif reconcile_booking_payment_state(db, booking):
        db.commit()
        db.refresh(booking)
    attach_booking_lifecycle_state(db, booking)
    return booking


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
    elif reconcile_gateway_payment_state(db, booking, attempts=2, delay_seconds=0.25):
        db.commit()
        db.refresh(booking)
    elif reconcile_booking_payment_state(db, booking):
        db.commit()
        db.refresh(booking)
    attach_booking_lifecycle_state(db, booking)
    return booking


def _cancel_booking(booking_id: int, db: Session) -> models.Booking:
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise booking_error(
            status_code=404,
            code=error_codes.HOLD_NOT_FOUND,
            message="Booking not found",
        )

    if expire_stale_booking_hold(booking):
        db.commit()
        db.refresh(booking)
        raise booking_error(
            status_code=400,
            code=error_codes.HOLD_EXPIRED,
            message="This booking hold has already expired",
        )

    if booking.status == models.BookingStatus.CANCELLED:
        raise booking_error(
            status_code=400,
            code=error_codes.HOLD_EXPIRED,
            message="This booking has already been cancelled",
        )
    if booking.status == models.BookingStatus.EXPIRED:
        raise booking_error(
            status_code=400,
            code=error_codes.HOLD_EXPIRED,
            message="This booking hold has already expired",
        )
    if booking.payment_status == models.PaymentStatus.PAID:
        raise booking_error(
            status_code=400,
            code=error_codes.PAYMENT_FAILED,
            message="Paid bookings cannot be cancelled this way. Please use the refund workflow.",
        )

    booking.status = models.BookingStatus.CANCELLED
    release_inventory_for_booking(db, booking=booking)
    queue_booking_cancellation_email(db, booking)
    db.commit()
    db.refresh(booking)
    clear_search_cache()

    # ── Real-time broadcast: booking cancelled ──
    _broadcast("booking-cancelled", {
        "booking_id": booking.id,
        "booking_ref": booking.booking_ref,
        "room_id": booking.room_id,
    }, source="customer")

    _broadcast("inventory-updated", {
        "room_id": booking.room_id,
        "action": "release",
        "booking_id": booking.id,
    }, source="system")

    return booking


@router.get("/{booking_id}/invoice")
def download_invoice(
    booking_id: int,
    booking_ref: Optional[str] = Query(None),
    current_user: Optional[models.User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    booking = get_booking_with_room_or_404(db, booking_id)
    if not can_access_booking_document(booking, current_user, booking_ref):
        raise booking_error(
            status_code=403,
            code=error_codes.AUTH_REQUIRED,
            message="You are not authorised to access this invoice",
        )
    invoice_pdf = build_invoice_pdf(booking)
    return build_document_response(invoice_pdf, f"{invoice_number_for_booking(booking)}.pdf")


@router.get("/{booking_id}/voucher")
def download_voucher(
    booking_id: int,
    booking_ref: Optional[str] = Query(None),
    current_user: Optional[models.User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    booking = get_booking_with_room_or_404(db, booking_id)
    if not can_access_booking_document(booking, current_user, booking_ref):
        raise booking_error(
            status_code=403,
            code=error_codes.AUTH_REQUIRED,
            message="You are not authorised to access this voucher",
        )
    voucher_pdf = build_voucher_pdf(booking)
    return build_document_response(voucher_pdf, f"VOUCHER-{booking.booking_ref}.pdf")


def _support_alert_recipient(booking: models.Booking) -> str:
    room = booking.room
    if room and room.partner_hotel and room.partner_hotel.support_email:
        return room.partner_hotel.support_email
    return settings.seed_admin_email or "support@stayvora.co.in"


@router.patch("/{booking_id}/cancel", response_model=schemas.BookingResponse)
def cancel_booking(booking_id: int, db: Session = Depends(get_db)):
    return _cancel_booking(booking_id, db)


@router.post("/{booking_id}/cancel", response_model=schemas.BookingResponse)
def cancel_booking_post(booking_id: int, db: Session = Depends(get_db)):
    return _cancel_booking(booking_id, db)


@router.post("/{booking_id}/support-request", response_model=schemas.MessageResponse)
def request_booking_support(
    booking_id: int,
    payload: schemas.BookingSupportRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.room).joinedload(models.Room.partner_hotel),
        )
        .filter(
            models.Booking.id == booking_id,
            or_(
                models.Booking.user_id == current_user.id,
                models.Booking.email == current_user.email,
            ),
        )
        .first()
    )
    if not booking:
        raise booking_error(
            status_code=404,
            code=error_codes.HOLD_NOT_FOUND,
            message="Booking not found",
        )

    queue_booking_support_request_email(
        db,
        recipient_email=_support_alert_recipient(booking),
        booking=booking,
        category=payload.category,
        message=payload.message,
    )
    write_audit_log(
        db,
        actor_user_id=current_user.id,
        action="booking.support_request",
        entity_type="booking",
        entity_id=booking.id,
        metadata={"category": payload.category},
    )
    db.commit()
    return {
        "message": "Support request submitted. Our team will contact you shortly."
    }


@router.get("/admin/dashboard", response_model=schemas.BookingDashboardResponse)
def get_booking_dashboard(
    email: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(get_current_admin),
) -> schemas.BookingDashboardResponse:
    query = db.query(models.Booking).options(joinedload(models.Booking.room))
    if email:
        query = query.filter(models.Booking.email.ilike(f"%{email}%"))
    if status:
        query = query.filter(models.Booking.status == status)
    if payment_status:
        query = query.filter(models.Booking.payment_status == payment_status)

    total = query.count()
    bookings = (
        query.order_by(models.Booking.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    pending_count = db.query(func.count(models.Booking.id)).filter(
        models.Booking.status == models.BookingStatus.PENDING
    ).scalar() or 0
    confirmed_count = db.query(func.count(models.Booking.id)).filter(
        models.Booking.status == models.BookingStatus.CONFIRMED
    ).scalar() or 0
    failed_payment_count = db.query(func.count(models.Booking.id)).filter(
        models.Booking.payment_status == models.PaymentStatus.FAILED
    ).scalar() or 0

    return schemas.BookingDashboardResponse(
        bookings=bookings,
        total=total,
        pending_count=pending_count,
        confirmed_count=confirmed_count,
        failed_payment_count=failed_payment_count,
    )
