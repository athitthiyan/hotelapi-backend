from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db
from routers.auth import get_current_admin
from services.audit_service import write_audit_log
from services.inventory_service import (
    calculate_effective_price,
    iter_stay_dates,
    release_expired_inventory_locks,
    upsert_inventory_range,
)
from services.search_service import (
    clear_search_cache,
    get_cached_search,
    make_search_cache_key,
    set_cached_search,
    sort_rooms,
)

router = APIRouter(prefix="/rooms", tags=["Rooms"])


def booking_overlaps_date_window(
    booking: models.Booking,
    *,
    from_date: date,
    to_date: date,
) -> bool:
    booking_start = booking.check_in.date()
    booking_end = booking.check_out.date()
    return booking_start <= to_date and booking_end > from_date


@router.get("", response_model=schemas.RoomListResponse)
def get_rooms(
    query: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    landmark: Optional[str] = Query(None),
    room_type: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    min_rating: Optional[float] = Query(None),
    amenities: Optional[str] = Query(None),
    guests: Optional[int] = Query(None),
    check_in: Optional[str] = Query(None),
    check_out: Optional[str] = Query(None),
    featured: Optional[bool] = Query(None),
    sort_by: str = Query("recommended"),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=50),
    db: Session = Depends(get_db),
):
    cache_key = make_search_cache_key(
        query=query,
        city=city,
        landmark=landmark,
        room_type=room_type,
        min_price=min_price,
        max_price=max_price,
        min_rating=min_rating,
        amenities=amenities,
        guests=guests,
        check_in=check_in,
        check_out=check_out,
        featured=featured,
        sort_by=sort_by,
        page=page,
        per_page=per_page,
    )
    cached = get_cached_search(cache_key)
    if cached:
        return cached

    room_query = db.query(models.Room).filter(
        models.Room.availability.is_(True),
        models.Room.is_active.is_(True),
        models.Room.deleted_at.is_(None),
    )

    if query:
        like_pattern = f"%{query}%"
        room_query = room_query.filter(
            (models.Room.hotel_name.ilike(like_pattern))
            | (models.Room.city.ilike(like_pattern))
            | (models.Room.country.ilike(like_pattern))
            | (models.Room.location.ilike(like_pattern))
        )
    if city:
        room_query = room_query.filter(models.Room.city.ilike(f"%{city}%"))
    if landmark:
        room_query = room_query.filter(models.Room.location.ilike(f"%{landmark}%"))
    if room_type:
        room_query = room_query.filter(models.Room.room_type == room_type)
    if min_price is not None:
        room_query = room_query.filter(models.Room.price >= min_price)
    if max_price is not None:
        room_query = room_query.filter(models.Room.price <= max_price)
    if min_rating is not None:
        room_query = room_query.filter(models.Room.rating >= min_rating)
    if guests:
        room_query = room_query.filter(models.Room.max_guests >= guests)
    if featured is not None:
        room_query = room_query.filter(models.Room.is_featured.is_(featured))
    if amenities:
        for amenity in [item.strip() for item in amenities.split(",") if item.strip()]:
            room_query = room_query.filter(models.Room.amenities.ilike(f"%{amenity}%"))

    if check_in and check_out:
        check_in_dt = datetime.fromisoformat(check_in)
        check_out_dt = datetime.fromisoformat(check_out)
        release_expired_inventory_locks(db)
        room_query = room_query.filter(
            ~db.query(models.RoomInventory.id)
            .filter(
                models.RoomInventory.room_id == models.Room.id,
                models.RoomInventory.inventory_date >= check_in_dt.date(),
                models.RoomInventory.inventory_date < check_out_dt.date(),
                or_(
                    models.RoomInventory.status == models.InventoryStatus.BLOCKED,
                    models.RoomInventory.available_units <= 0,
                ),
            )
            .exists()
        )

    rooms = room_query.all()
    if check_in:
        check_in_date = datetime.fromisoformat(check_in).date()
        inventory_rows = []
        if rooms:
            inventory_rows = (
                db.query(models.RoomInventory)
                .filter(
                    models.RoomInventory.room_id.in_([room.id for room in rooms]),
                    models.RoomInventory.inventory_date == check_in_date,
                )
                .all()
            )
        by_room = {row.room_id: row for row in inventory_rows}
        for room in rooms:
            room.price = calculate_effective_price(
                room,
                inventory_date=check_in_date,
                inventory_row=by_room.get(room.id),
            )
    rooms = sort_rooms(rooms, sort_by)
    total = len(rooms)
    paginated_rooms = rooms[(page - 1) * per_page : page * per_page]

    payload = {"rooms": paginated_rooms, "total": total, "page": page, "per_page": per_page}
    set_cached_search(cache_key, payload)
    return payload


@router.get("/featured", response_model=list[schemas.RoomResponse])
def get_featured_rooms(limit: int = Query(6), db: Session = Depends(get_db)):
    return db.query(models.Room).filter(
        models.Room.is_featured.is_(True),
        models.Room.availability.is_(True),
        models.Room.is_active.is_(True),
        models.Room.deleted_at.is_(None),
    ).limit(limit).all()


@router.get("/destinations", response_model=schemas.DestinationListResponse)
def get_destinations(
    query: Optional[str] = Query(None),
    limit: int = Query(12, ge=1, le=50),
    db: Session = Depends(get_db),
):
    destination_query = (
        db.query(
            models.Room.city.label("city"),
            models.Room.country.label("country"),
            func.count(models.Room.id).label("room_count"),
            func.sum(case((models.Room.is_featured.is_(True), 1), else_=0)).label(
                "featured_count"
            ),
            func.avg(models.Room.price).label("average_price"),
        )
        .filter(
            models.Room.availability.is_(True),
            models.Room.is_active.is_(True),
            models.Room.deleted_at.is_(None),
        )
        .group_by(models.Room.city, models.Room.country)
    )

    if query:
        like_pattern = f"%{query}%"
        destination_query = destination_query.filter(
            (models.Room.city.ilike(like_pattern))
            | (models.Room.country.ilike(like_pattern))
        )

    rows = (
        destination_query.order_by(
            func.count(models.Room.id).desc(),
            func.avg(models.Room.price).asc(),
        )
        .limit(limit)
        .all()
    )

    destinations = [
        schemas.DestinationResponse(
            city=row.city,
            country=row.country,
            room_count=row.room_count,
            featured_count=int(row.featured_count or 0),
            average_price=round(float(row.average_price or 0), 2),
        )
        for row in rows
    ]
    return {"destinations": destinations, "total": len(destinations)}


@router.get("/{room_id}/unavailable-dates", response_model=schemas.UnavailableDatesResponse)
def get_room_unavailable_dates(
    room_id: int,
    from_date: Optional[date] = Query(default=None),
    to_date: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Return the set of dates (in ISO format) that are unavailable for booking.

    * **unavailable_dates** — permanently blocked or confirmed; will not free up.
    * **held_dates** — locked by an active inventory hold; *may* free up when the
      hold expires.

    Defaults to a 180-day window starting from today.
    """
    utc_today = datetime.now(timezone.utc).date()
    if from_date is None:
        from_date = utc_today
    if to_date is None:
        to_date = utc_today + timedelta(days=180)

    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must be >= from_date")

    room = db.query(models.Room).filter(
        models.Room.id == room_id,
        models.Room.deleted_at.is_(None),
        models.Room.is_active.is_(True),
    ).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    # Clean up expired locks so we report an accurate picture
    release_expired_inventory_locks(db, room_id=room_id)

    # -----------------------------------------------------------------
    # 1.  Dates from the inventory table (locked or blocked rows)
    # -----------------------------------------------------------------
    inventory_rows = (
        db.query(models.RoomInventory)
        .filter(
            models.RoomInventory.room_id == room_id,
            models.RoomInventory.inventory_date >= from_date,
            models.RoomInventory.inventory_date <= to_date,
        )
        .all()
    )

    unavailable_dates: set[str] = set()
    held_dates: set[str] = set()
    now = datetime.now(timezone.utc)

    for row in inventory_rows:
        date_str = row.inventory_date.isoformat()
        if row.available_units <= 0 and row.locked_units == 0:
            # Permanently blocked (e.g. confirmed, admin-blocked)
            unavailable_dates.add(date_str)
        elif row.locked_units > 0 and row.lock_expires_at is not None:
            # Active temporary hold
            lock_exp = row.lock_expires_at
            if lock_exp.tzinfo is None:
                lock_exp = lock_exp.replace(tzinfo=timezone.utc)
            if lock_exp > now:
                held_dates.add(date_str)
            # if expired but release_expired_inventory_locks didn't catch it yet → skip
        elif row.available_units <= 0:
            # No units, no active lock — treat as unavailable
            unavailable_dates.add(date_str)

    # -----------------------------------------------------------------
    # 2.  Expand CONFIRMED bookings into individual night dates
    #     (these might not have inventory rows if inventory was never
    #     initialised for those dates)
    # -----------------------------------------------------------------
    confirmed_bookings = (
        db.query(models.Booking)
        .filter(
            models.Booking.room_id == room_id,
            models.Booking.status == models.BookingStatus.CONFIRMED,
        )
        .all()
    )
    for booking in confirmed_bookings:
        if not booking_overlaps_date_window(
            booking,
            from_date=from_date,
            to_date=to_date,
        ):
            continue
        for stay_date in iter_stay_dates(booking.check_in, booking.check_out):
            if from_date <= stay_date <= to_date:
                unavailable_dates.add(stay_date.isoformat())
                # Remove from held if we also have it there (confirmed takes priority)
                held_dates.discard(stay_date.isoformat())

    return schemas.UnavailableDatesResponse(
        unavailable_dates=sorted(unavailable_dates),
        held_dates=sorted(held_dates - unavailable_dates),
    )


@router.get("/{room_id}", response_model=schemas.RoomResponse)
def get_room(room_id: int, db: Session = Depends(get_db)):
    room = db.query(models.Room).filter(
        models.Room.id == room_id,
        models.Room.deleted_at.is_(None),
        models.Room.is_active.is_(True),
    ).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@router.post("", response_model=schemas.RoomResponse, status_code=201)
def create_room(
    room: schemas.RoomCreate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    db_room = models.Room(**room.model_dump())
    db.add(db_room)
    db.flush()
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="room.create",
        entity_type="room",
        entity_id=db_room.id,
        metadata={"hotel_name": db_room.hotel_name, "city": db_room.city},
    )
    db.commit()
    db.refresh(db_room)
    clear_search_cache()
    return db_room


@router.patch("/{room_id}", response_model=schemas.RoomResponse)
def update_room(
    room_id: int,
    payload: schemas.RoomUpdate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    room = db.query(models.Room).filter(models.Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(room, field, value)
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="room.update",
        entity_type="room",
        entity_id=room.id,
        metadata={"updated_fields": sorted(updates.keys())},
    )
    db.commit()
    db.refresh(room)
    clear_search_cache()
    return room


@router.delete("/{room_id}")
def delete_room(
    room_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    room = db.query(models.Room).filter(models.Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if db.query(models.Booking).filter(models.Booking.room_id == room_id).count() > 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a room with existing bookings",
        )

    db.query(models.RoomInventory).filter(models.RoomInventory.room_id == room_id).delete()
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="room.delete",
        entity_type="room",
        entity_id=room.id,
        metadata={"hotel_name": room.hotel_name},
    )
    db.delete(room)
    db.commit()
    clear_search_cache()
    return {"message": "Room deleted successfully"}


@router.post("/inventory", response_model=schemas.InventoryListResponse)
def update_room_inventory(
    payload: schemas.InventoryUpdateRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    room = db.query(models.Room).filter(models.Room.id == payload.room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if payload.total_units < 0:
        raise HTTPException(status_code=400, detail="Total units cannot be negative")
    if payload.available_units is not None and payload.available_units < 0:
        raise HTTPException(status_code=400, detail="Available units cannot be negative")

    rows = upsert_inventory_range(
        db,
        room_id=payload.room_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        total_units=payload.total_units,
        available_units=payload.available_units,
        status=models.InventoryStatus(payload.status.value),
    )
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="inventory.update",
        entity_type="room",
        entity_id=payload.room_id,
        metadata={
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "total_units": payload.total_units,
            "available_units": payload.available_units,
            "status": payload.status.value,
        },
    )
    clear_search_cache()
    return {"inventory": rows, "total": len(rows)}


@router.get("/{room_id}/inventory", response_model=schemas.InventoryListResponse)
def get_room_inventory(
    room_id: int,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    room = db.query(models.Room).filter(models.Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    query = db.query(models.RoomInventory).filter(models.RoomInventory.room_id == room_id)
    if start_date:
        query = query.filter(models.RoomInventory.inventory_date >= datetime.fromisoformat(start_date).date())
    if end_date:
        query = query.filter(models.RoomInventory.inventory_date <= datetime.fromisoformat(end_date).date())
    rows = query.order_by(models.RoomInventory.inventory_date.asc()).all()
    return {"inventory": rows, "total": len(rows)}
