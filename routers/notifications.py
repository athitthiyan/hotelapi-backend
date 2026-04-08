from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db
from routers.auth import get_current_admin, get_current_user
from services.notification_service import process_pending_notifications

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# ── In-App Notifications (admin dashboard) ─────────────────────────

@router.get("")
def get_admin_notifications(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Fetch in-app notifications for the current user (admin or partner)."""
    notifications = (
        db.query(models.AdminNotification)
        .filter(models.AdminNotification.user_id == current_user.id)
        .order_by(models.AdminNotification.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "notifications": [
            {
                "id": n.id,
                "type": n.type,
                "title": n.title,
                "message": n.message,
                "read": n.read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
                "metadata": n.metadata_json,
            }
            for n in notifications
        ]
    }


@router.patch("/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notif = (
        db.query(models.AdminNotification)
        .filter(models.AdminNotification.id == notification_id, models.AdminNotification.user_id == current_user.id)
        .first()
    )
    if notif:
        notif.read = True
        db.commit()
    return {"status": "ok"}


@router.patch("/read-all")
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    db.query(models.AdminNotification).filter(
        models.AdminNotification.user_id == current_user.id,
        ~models.AdminNotification.read,
    ).update({"read": True})
    db.commit()
    return {"status": "ok"}


# ── Helper: create notification ─────────────────────────────────────

def create_admin_notification(
    db: Session,
    user_id: int,
    notification_type: str,
    title: str,
    message: str,
    metadata: dict | None = None,
):
    """Create an in-app notification for a specific admin/user."""
    notif = models.AdminNotification(
        user_id=user_id,
        type=notification_type,
        title=title,
        message=message,
        read=False,
        metadata_json=metadata,
        created_at=datetime.now(timezone.utc),
    )
    db.add(notif)
    db.commit()
    return notif


# ── Email Outbox Endpoints ──────────────────────────────────────────

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
