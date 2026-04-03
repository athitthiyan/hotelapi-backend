from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from typing import Optional
from datetime import datetime
import models, schemas
from database import get_db
from routers.auth import get_current_admin

router = APIRouter(prefix="/rooms", tags=["Rooms"])


@router.get("", response_model=schemas.RoomListResponse)
def get_rooms(
    city: Optional[str] = Query(None),
    room_type: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    guests: Optional[int] = Query(None),
    check_in: Optional[str] = Query(None),
    check_out: Optional[str] = Query(None),
    featured: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=50),
    db: Session = Depends(get_db),
):
    query = db.query(models.Room).filter(models.Room.availability == True)

    if city:
        query = query.filter(models.Room.city.ilike(f"%{city}%"))
    if room_type:
        query = query.filter(models.Room.room_type == room_type)
    if min_price is not None:
        query = query.filter(models.Room.price >= min_price)
    if max_price is not None:
        query = query.filter(models.Room.price <= max_price)
    if guests:
        query = query.filter(models.Room.max_guests >= guests)
    if featured is not None:
        query = query.filter(models.Room.is_featured == featured)

    total = query.count()
    rooms = query.offset((page - 1) * per_page).limit(per_page).all()

    return {"rooms": rooms, "total": total, "page": page, "per_page": per_page}


@router.get("/featured", response_model=list[schemas.RoomResponse])
def get_featured_rooms(limit: int = Query(6), db: Session = Depends(get_db)):
    return db.query(models.Room).filter(
        models.Room.is_featured == True,
        models.Room.availability == True
    ).limit(limit).all()


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
    return db_room
