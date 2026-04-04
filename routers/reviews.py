"""Reviews router — verified-stay reviews with host-reply support."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db
from routers.auth import get_current_admin, get_current_user

router = APIRouter(prefix="/reviews", tags=["Reviews"])


def _build_rating_breakdown(reviews: list[models.Review]) -> dict:
    if not reviews:
        return {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    counts: dict[str, int] = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for r in reviews:
        counts[str(r.rating)] += 1
    return counts


def _calc_avg(values: list[Optional[int]]) -> Optional[float]:
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return round(sum(valid) / len(valid), 2)


def _review_to_response(review: models.Review) -> schemas.ReviewResponse:
    reviewer_name = review.user.full_name if review.user else "Guest"
    return schemas.ReviewResponse(
        id=review.id,
        user_id=review.user_id,
        room_id=review.room_id,
        booking_id=review.booking_id,
        rating=review.rating,
        cleanliness_rating=review.cleanliness_rating,
        service_rating=review.service_rating,
        value_rating=review.value_rating,
        location_rating=review.location_rating,
        title=review.title,
        body=review.body,
        is_verified=review.is_verified,
        host_reply=review.host_reply,
        host_replied_at=review.host_replied_at,
        reviewer_name=reviewer_name,
        created_at=review.created_at,
    )


@router.get("/rooms/{room_id}", response_model=schemas.ReviewListResponse)
def get_room_reviews(
    room_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Return paginated reviews for a specific room."""
    room = db.query(models.Room).filter(models.Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    all_reviews = (
        db.query(models.Review)
        .options(joinedload(models.Review.user))
        .filter(models.Review.room_id == room_id)
        .order_by(models.Review.created_at.desc())
        .all()
    )
    total = len(all_reviews)
    start = (page - 1) * per_page
    paginated = all_reviews[start : start + per_page]

    avg_rating = round(sum(r.rating for r in all_reviews) / total, 2) if total else 0.0
    breakdown = _build_rating_breakdown(all_reviews)

    return schemas.ReviewListResponse(
        reviews=[_review_to_response(r) for r in paginated],
        total=total,
        average_rating=avg_rating,
        rating_breakdown=breakdown,
    )


@router.post("", response_model=schemas.ReviewResponse, status_code=201)
def create_review(
    payload: schemas.ReviewCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a verified review — requires a completed booking owned by the user."""
    booking = (
        db.query(models.Booking)
        .filter(
            models.Booking.id == payload.booking_id,
            models.Booking.email == current_user.email,
            models.Booking.room_id == payload.room_id,
        )
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found or does not belong to you")

    if booking.status not in (
        models.BookingStatus.CONFIRMED,
        models.BookingStatus.COMPLETED,
    ):
        raise HTTPException(
            status_code=422,
            detail="You can only review a confirmed or completed booking",
        )

    existing = (
        db.query(models.Review)
        .filter(models.Review.booking_id == payload.booking_id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="A review for this booking already exists")

    review = models.Review(
        user_id=current_user.id,
        room_id=payload.room_id,
        booking_id=payload.booking_id,
        rating=payload.rating,
        cleanliness_rating=payload.cleanliness_rating,
        service_rating=payload.service_rating,
        value_rating=payload.value_rating,
        location_rating=payload.location_rating,
        title=payload.title,
        body=payload.body,
        is_verified=True,
    )
    db.add(review)

    # Update denormalised room rating
    _refresh_room_rating(db, payload.room_id, review)

    db.commit()
    db.refresh(review)

    return _review_to_response(review)


@router.post("/{review_id}/host-reply", response_model=schemas.ReviewResponse)
def host_reply(
    review_id: int,
    payload: schemas.HostReplyRequest,
    admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Admin/host can reply to a review."""
    from datetime import timezone

    review = db.query(models.Review).options(joinedload(models.Review.user)).filter(
        models.Review.id == review_id
    ).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    review.host_reply = payload.reply
    review.host_replied_at = __import__("datetime").datetime.now(timezone.utc)
    db.commit()
    db.refresh(review)
    return _review_to_response(review)


@router.delete("/{review_id}", status_code=204)
def delete_review(
    review_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """User deletes their own review (or admin deletes any)."""
    review = db.query(models.Review).filter(models.Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    if review.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Not authorised to delete this review")
    db.delete(review)
    db.commit()


def _refresh_room_rating(db: Session, room_id: int, new_review: models.Review) -> None:
    """Recompute and persist the denormalised rating on the Room row."""
    agg = (
        db.query(func.avg(models.Review.rating), func.count(models.Review.id))
        .filter(models.Review.room_id == room_id)
        .one()
    )
    avg_rating, count = agg
    if avg_rating is not None:
        room = db.query(models.Room).filter(models.Room.id == room_id).first()
        if room:
            room.rating = round(float(avg_rating), 2)
            room.review_count = int(count) + 1
