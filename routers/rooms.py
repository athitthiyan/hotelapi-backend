from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import case, func
from typing import Optional
from datetime import datetime
import models, schemas
from database import get_db
from routers.auth import get_current_admin
from services.inventory_service import is_inventory_available, upsert_inventory_range
from services.search_service import (
    clear_search_cache,
    get_cached_search,
    make_search_cache_key,
    set_cached_search,
    sort_rooms,
)

router = APIRouter(prefix="/rooms", tags=["Rooms"])


@router.get("", response_model=schemas.RoomListResponse)
def get_rooms(
    query: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    room_type: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
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
        room_type=room_type,
        min_price=min_price,
        max_price=max_price,
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

    room_query = db.query(models.Room).filter(models.Room.availability == True)

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
    if room_type:
        room_query = room_query.filter(models.Room.room_type == room_type)
    if min_price is not None:
        room_query = room_query.filter(models.Room.price >= min_price)
    if max_price is not None:
        room_query = room_query.filter(models.Room.price <= max_price)
    if guests:
        room_query = room_query.filter(models.Room.max_guests >= guests)
    if featured is not None:
        room_query = room_query.filter(models.Room.is_featured == featured)

    rooms = room_query.all()
    if check_in and check_out:
        check_in_dt = datetime.fromisoformat(check_in)
        check_out_dt = datetime.fromisoformat(check_out)
        rooms = [
            room
            for room in rooms
            if is_inventory_available(
                db, room_id=room.id, check_in=check_in_dt, check_out=check_out_dt
            )
        ]

    rooms = sort_rooms(rooms, sort_by)
    total = len(rooms)
    paginated_rooms = rooms[(page - 1) * per_page : page * per_page]

    payload = {"rooms": paginated_rooms, "total": total, "page": page, "per_page": per_page}
    set_cached_search(cache_key, payload)
    return payload


@router.get("/featured", response_model=list[schemas.RoomResponse])
def get_featured_rooms(limit: int = Query(6), db: Session = Depends(get_db)):
    return db.query(models.Room).filter(
        models.Room.is_featured == True,
        models.Room.availability == True
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
            func.sum(case((models.Room.is_featured == True, 1), else_=0)).label(
                "featured_count"
            ),
            func.avg(models.Room.price).label("average_price"),
        )
        .filter(models.Room.availability == True)
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


@router.get("/{room_id}", response_model=schemas.RoomResponse)
def get_room(room_id: int, db: Session = Depends(get_db)):
    room = db.query(models.Room).filter(models.Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@router.post("", response_model=schemas.RoomResponse, status_code=201)
def create_room(
    room: schemas.RoomCreate,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    db_room = models.Room(**room.model_dump())
    db.add(db_room)
    db.commit()
    db.refresh(db_room)
    clear_search_cache()
    return db_room


@router.post("/inventory", response_model=schemas.InventoryListResponse)
def update_room_inventory(
    payload: schemas.InventoryUpdateRequest,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
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
