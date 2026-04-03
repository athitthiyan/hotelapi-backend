from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import models
import schemas
from database import engine, get_db
from routers.auth import get_current_admin
from services.audit_service import write_audit_log
from services.worker_service import get_operational_counts, run_maintenance_cycle

router = APIRouter(tags=["Operations"])


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
