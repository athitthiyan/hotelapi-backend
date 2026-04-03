from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract, cast, Date
from typing import Optional
from datetime import datetime, timedelta
import models, schemas
from database import get_db
from routers.auth import get_current_admin

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("", response_model=schemas.AnalyticsResponse)
def get_analytics(
    days: int = Query(30, ge=7, le=365),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    now = datetime.utcnow()
    start_date = now - timedelta(days=days)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── KPIs ──────────────────────────────────────────────────────────────────
    total_bookings = db.query(func.count(models.Booking.id)).scalar()

    paid_transactions = db.query(models.Transaction).filter(
        models.Transaction.status == models.TransactionStatus.SUCCESS
    )
    total_revenue = db.query(
        func.coalesce(func.sum(models.Transaction.amount), 0)
    ).filter(
        models.Transaction.status == models.TransactionStatus.SUCCESS
    ).scalar()

    all_txn_count = db.query(func.count(models.Transaction.id)).scalar()
    success_txn_count = db.query(func.count(models.Transaction.id)).filter(
        models.Transaction.status == models.TransactionStatus.SUCCESS
    ).scalar()
    success_rate = round((success_txn_count / all_txn_count * 100) if all_txn_count > 0 else 0, 1)

    avg_booking_value = round(total_revenue / success_txn_count if success_txn_count > 0 else 0, 2)

    bookings_today = db.query(func.count(models.Booking.id)).filter(
        models.Booking.created_at >= today_start
    ).scalar()

    revenue_today = db.query(
        func.coalesce(func.sum(models.Transaction.amount), 0)
    ).filter(
        models.Transaction.status == models.TransactionStatus.SUCCESS,
        models.Transaction.created_at >= today_start,
    ).scalar()

    pending_bookings = db.query(func.count(models.Booking.id)).filter(
        models.Booking.status == models.BookingStatus.PENDING
    ).scalar()

    failed_payments = db.query(func.count(models.Transaction.id)).filter(
        models.Transaction.status == models.TransactionStatus.FAILED
    ).scalar()

    kpis = schemas.KPIStats(
        total_bookings=total_bookings or 0,
        total_revenue=float(total_revenue or 0),
        success_rate=success_rate,
        avg_booking_value=float(avg_booking_value or 0),
        bookings_today=bookings_today or 0,
        revenue_today=float(revenue_today or 0),
        pending_bookings=pending_bookings or 0,
        failed_payments=failed_payments or 0,
    )

    # ── Daily Stats ───────────────────────────────────────────────────────────
    daily_raw = db.query(
        cast(models.Booking.created_at, Date).label("date"),
        func.count(models.Booking.id).label("bookings"),
    ).filter(models.Booking.created_at >= start_date)\
     .group_by(cast(models.Booking.created_at, Date))\
     .order_by(cast(models.Booking.created_at, Date)).all()

    daily_revenue_raw = db.query(
        cast(models.Transaction.created_at, Date).label("date"),
        func.coalesce(func.sum(models.Transaction.amount), 0).label("revenue"),
    ).filter(
        models.Transaction.status == models.TransactionStatus.SUCCESS,
        models.Transaction.created_at >= start_date,
    ).group_by(cast(models.Transaction.created_at, Date))\
     .order_by(cast(models.Transaction.created_at, Date)).all()

    revenue_map = {str(r.date): float(r.revenue) for r in daily_revenue_raw}
    daily_stats = [
        schemas.DailyStats(
            date=str(r.date),
            bookings=r.bookings,
            revenue=revenue_map.get(str(r.date), 0.0),
        )
        for r in daily_raw
    ]

    # ── Monthly Revenue ───────────────────────────────────────────────────────
    monthly_raw = db.query(
        extract("year", models.Transaction.created_at).label("year"),
        extract("month", models.Transaction.created_at).label("month"),
        func.coalesce(func.sum(models.Transaction.amount), 0).label("revenue"),
        func.count(models.Booking.id).label("bookings"),
    ).join(models.Booking, models.Transaction.booking_id == models.Booking.id)\
     .filter(models.Transaction.status == models.TransactionStatus.SUCCESS)\
     .group_by("year", "month")\
     .order_by("year", "month")\
     .limit(12).all()

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly_revenue = [
        schemas.MonthlyRevenue(
            month=f"{month_names[int(r.month) - 1]} {int(r.year)}",
            revenue=float(r.revenue),
            bookings=r.bookings,
        )
        for r in monthly_raw
    ]

    # ── Payment Status Breakdown ──────────────────────────────────────────────
    status_raw = db.query(
        models.Transaction.status,
        func.count(models.Transaction.id).label("count"),
    ).group_by(models.Transaction.status).all()

    total_txn = sum(r.count for r in status_raw) or 1
    payment_breakdown = [
        schemas.PaymentStatusBreakdown(
            status=r.status.value,
            count=r.count,
            percentage=round(r.count / total_txn * 100, 1),
        )
        for r in status_raw
    ]

    # ── Room Type Breakdown ───────────────────────────────────────────────────
    room_type_raw = db.query(
        models.Room.room_type,
        func.count(models.Booking.id).label("count"),
        func.coalesce(func.sum(models.Transaction.amount), 0).label("revenue"),
    ).join(models.Booking, models.Room.id == models.Booking.room_id)\
     .outerjoin(models.Transaction, models.Booking.id == models.Transaction.booking_id)\
     .group_by(models.Room.room_type).all()

    room_type_breakdown = [
        schemas.RoomTypeBreakdown(
            room_type=r.room_type.value,
            count=r.count,
            revenue=float(r.revenue),
        )
        for r in room_type_raw
    ]

    return schemas.AnalyticsResponse(
        kpis=kpis,
        daily_stats=daily_stats,
        monthly_revenue=monthly_revenue,
        payment_breakdown=payment_breakdown,
        room_type_breakdown=room_type_breakdown,
    )


@router.get("/recent-bookings", response_model=schemas.BookingListResponse)
def get_recent_bookings(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    from sqlalchemy.orm import joinedload
    bookings = db.query(models.Booking).options(
        joinedload(models.Booking.room)
    ).order_by(models.Booking.created_at.desc()).limit(limit).all()
    return {"bookings": bookings, "total": len(bookings)}


@router.get("/revenue-stats")
def get_revenue_stats(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    this_month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
    last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)

    this_month = db.query(
        func.coalesce(func.sum(models.Transaction.amount), 0)
    ).filter(
        models.Transaction.status == models.TransactionStatus.SUCCESS,
        models.Transaction.created_at >= this_month_start,
    ).scalar()

    last_month = db.query(
        func.coalesce(func.sum(models.Transaction.amount), 0)
    ).filter(
        models.Transaction.status == models.TransactionStatus.SUCCESS,
        models.Transaction.created_at >= last_month_start,
        models.Transaction.created_at < this_month_start,
    ).scalar()

    growth = 0.0
    if last_month and last_month > 0:
        growth = round(((float(this_month) - float(last_month)) / float(last_month)) * 100, 1)

    return {
        "this_month": float(this_month or 0),
        "last_month": float(last_month or 0),
        "growth_percentage": growth,
    }
