from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db
from routers.auth import get_current_admin
from services.notification_service import process_pending_notifications

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("/outbox", response_model=schemas.NotificationListResponse)
def get_notification_outbox(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    query = db.query(models.NotificationOutbox)
    if status:
        query = query.filter(models.NotificationOutbox.status == status)

    notifications = (
        query.order_by(models.NotificationOutbox.created_at.desc(), models.NotificationOutbox.id.desc())
        .limit(limit)
        .all()
    )
    return {"notifications": notifications, "total": len(notifications)}


@router.post("/process", response_model=schemas.ProcessNotificationsResponse)
def process_outbox(
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin),
):
    return process_pending_notifications(db, limit=limit)
