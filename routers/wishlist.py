"""Wishlist router — save / unsave rooms, fetch saved list."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

import models
import schemas
from database import get_db
from routers.auth import get_current_user

router = APIRouter(prefix="/wishlist", tags=["Wishlist"])


@router.post("/{room_id}", response_model=schemas.WishlistToggleResponse)
def toggle_wishlist(
    room_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle save/unsave — idempotent heart button."""
    room = db.query(models.Room).filter(models.Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    existing = (
        db.query(models.Wishlist)
        .filter(
            models.Wishlist.user_id == current_user.id,
            models.Wishlist.room_id == room_id,
        )
        .first()
    )
    if existing:
        db.delete(existing)
        db.commit()
        return schemas.WishlistToggleResponse(saved=False, message="Removed from wishlist")

    entry = models.Wishlist(user_id=current_user.id, room_id=room_id)
    try:
        db.add(entry)
        db.commit()
    except IntegrityError:
        db.rollback()
        return schemas.WishlistToggleResponse(saved=True, message="Already saved")

    return schemas.WishlistToggleResponse(saved=True, message="Added to wishlist")


@router.get("", response_model=schemas.WishlistResponse)
def get_wishlist(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all saved rooms for the authenticated user."""
    items = (
        db.query(models.Wishlist)
        .options(joinedload(models.Wishlist.room))
        .filter(models.Wishlist.user_id == current_user.id)
        .order_by(models.Wishlist.created_at.desc())
        .all()
    )
    return schemas.WishlistResponse(
        items=[
            schemas.WishlistItemResponse(
                id=item.id,
                room_id=item.room_id,
                room=item.room,
                created_at=item.created_at,
            )
            for item in items
        ],
        total=len(items),
    )


@router.get("/status", response_model=schemas.WishlistStatusResponse)
def wishlist_status(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all room_ids saved by the user — used to pre-fill heart icons."""
    rows = (
        db.query(models.Wishlist.room_id)
        .filter(models.Wishlist.user_id == current_user.id)
        .all()
    )
    return schemas.WishlistStatusResponse(room_ids=[r[0] for r in rows])


@router.delete("/{room_id}", status_code=204)
def remove_from_wishlist(
    room_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Explicit remove endpoint (alternative to toggle)."""
    entry = (
        db.query(models.Wishlist)
        .filter(
            models.Wishlist.user_id == current_user.id,
            models.Wishlist.room_id == room_id,
        )
        .first()
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Room not in wishlist")
    db.delete(entry)
    db.commit()
