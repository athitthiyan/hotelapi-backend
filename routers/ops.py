from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import models
import schemas
from database import engine, get_db
from routers.auth import get_current_admin
from services.audit_service import write_audit_log
from services.inventory_service import confirm_inventory_for_booking, release_inventory_for_booking
from services.worker_service import get_operational_counts, run_maintenance_cycle

router = APIRouter(tags=["Operations"])


def _incident_summary(
    booking: models.Booking,
    transaction_ref: str | None = None,
) -> schemas.IncidentBookingSummary:
    return schemas.IncidentBookingSummary(
        booking_id=booking.id,
        booking_ref=booking.booking_ref,
        status=booking.status,
        payment_status=booking.payment_status,
        room_id=booking.room_id,
        email=booking.email,
        transaction_ref=transaction_ref,
        created_at=booking.created_at,
        hold_expires_at=booking.hold_expires_at,
    )


@router.get("/ready", response_model=schemas.ReadinessResponse)
def readiness_check(db: Session = Depends(get_db)):
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_status = "connected"
        status = "ready"
    except SQLAlchemyError:
        database_status = "unavailable"
        status = "degraded"

    counts = get_operational_counts(db)
    return {
        "status": status,
        "service": "hotel-api",
        "database": database_status,
        "pending_notifications": counts["pending_notifications"],
        "processing_payments": counts["processing_payments"],
    }


@router.post("/ops/run-maintenance", response_model=schemas.MaintenanceRunResponse)
def run_maintenance(
    payment_timeout_minutes: int = Query(30, ge=1, le=1440),
    notification_limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    result = run_maintenance_cycle(
        db,
        payment_timeout_minutes=payment_timeout_minutes,
        notification_limit=notification_limit,
    )
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="ops.maintenance.run",
        entity_type="system",
        entity_id="maintenance-cycle",
        metadata=result,
    )
    db.commit()
    return result


@router.get("/ops/audit-logs", response_model=schemas.AuditLogListResponse)
def get_audit_logs(
    action: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    query = db.query(models.AuditLog)
    if action:
        query = query.filter(models.AuditLog.action == action)
    logs = (
        query.order_by(models.AuditLog.created_at.desc(), models.AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    return {"logs": logs, "total": len(logs)}


@router.get("/ops/incidents", response_model=schemas.IncidentDashboardResponse)
def get_incident_dashboard(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    success_transactions = (
        db.query(models.Transaction)
        .filter(models.Transaction.status == models.TransactionStatus.SUCCESS)
        .all()
    )
    success_by_booking = {transaction.booking_id: transaction for transaction in success_transactions}

    orphan_paid_bookings = []
    for booking in (
        db.query(models.Booking)
        .filter(models.Booking.payment_status == models.PaymentStatus.PAID)
        .order_by(models.Booking.created_at.desc(), models.Booking.id.desc())
        .limit(25)
        .all()
    ):
        transaction = success_by_booking.get(booking.id)
        if transaction:
            continue
        orphan_paid_bookings.append(_incident_summary(booking))

    stale_processing_bookings = []
    for booking in (
        db.query(models.Booking)
        .filter(
            models.Booking.status == models.BookingStatus.PROCESSING,
            models.Booking.payment_status == models.PaymentStatus.PROCESSING,
        )
        .order_by(models.Booking.created_at.desc(), models.Booking.id.desc())
        .limit(25)
        .all()
    ):
        transaction = (
            db.query(models.Transaction)
            .filter(models.Transaction.booking_id == booking.id)
            .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
            .first()
        )
        stale_processing_bookings.append(
            _incident_summary(booking, transaction_ref=transaction.transaction_ref if transaction else None)
        )

    active_holds = [
        _incident_summary(booking)
        for booking in (
            db.query(models.Booking)
            .filter(
                models.Booking.status.in_([models.BookingStatus.PENDING, models.BookingStatus.PROCESSING]),
                models.Booking.hold_expires_at.is_not(None),
            )
            .order_by(models.Booking.created_at.desc(), models.Booking.id.desc())
            .limit(25)
            .all()
        )
        if booking.hold_expires_at
    ]

    return {
        "orphan_paid_bookings": orphan_paid_bookings,
        "stale_processing_bookings": stale_processing_bookings,
        "active_holds": active_holds,
    }


@router.post("/ops/bookings/{booking_id}/release-hold", response_model=schemas.BookingResponse)
def release_hold_for_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_status == models.PaymentStatus.PAID:
        raise HTTPException(
            status_code=409,
            detail="Paid bookings cannot have their hold released manually",
        )

    booking.status = models.BookingStatus.CANCELLED
    release_inventory_for_booking(db, booking=booking)
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="ops.booking.release_hold",
        entity_type="booking",
        entity_id=booking.id,
        metadata={"booking_ref": booking.booking_ref},
    )
    db.commit()
    db.refresh(booking)
    return booking


@router.post("/ops/bookings/{booking_id}/force-confirm", response_model=schemas.BookingResponse)
def force_confirm_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    transaction = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.booking_id == booking.id,
            models.Transaction.status == models.TransactionStatus.SUCCESS,
        )
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )
    if not transaction:
        raise HTTPException(
            status_code=409,
            detail="Cannot force confirm without a successful transaction",
        )

    booking.payment_status = models.PaymentStatus.PAID
    booking.status = models.BookingStatus.CONFIRMED
    confirm_inventory_for_booking(db, booking=booking)
    write_audit_log(
        db,
        actor_user_id=admin.id,
        action="ops.booking.force_confirm",
        entity_type="booking",
        entity_id=booking.id,
        metadata={"booking_ref": booking.booking_ref, "transaction_ref": transaction.transaction_ref},
    )
    db.commit()
    db.refresh(booking)
    return booking
