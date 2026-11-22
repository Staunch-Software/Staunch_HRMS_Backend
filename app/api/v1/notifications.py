"""The signed-in user's own notifications (HR and employee logins alike)."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.notification import Notification
from app.models.user import AdminUser
from app.schemas.notification import NotificationList, NotificationRead, ReadAllResult, UnreadCount

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _unread_count(db: Session, user_id: int) -> int:
    return db.query(func.count(Notification.id)).filter(
        Notification.user_id == user_id, Notification.is_read.is_(False)
    ).scalar() or 0


@router.get("", response_model=NotificationList)
def list_notifications(
    unread_only: bool = False,
    limit: int = Query(20, ge=1, le=100),
    before_id: Optional[int] = None,
    user: AdminUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        query = query.filter(Notification.is_read.is_(False))
    if before_id is not None:
        query = query.filter(Notification.id < before_id)
    items = query.order_by(Notification.id.desc()).limit(limit).all()
    return NotificationList(items=items, unread_count=_unread_count(db, user.id))


@router.get("/unread-count", response_model=UnreadCount)
def unread_count(user: AdminUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return UnreadCount(unread_count=_unread_count(db, user.id))


@router.post("/read-all", response_model=ReadAllResult)
def mark_all_read(user: AdminUser = Depends(get_current_user), db: Session = Depends(get_db)):
    result = db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return ReadAllResult(updated=result.rowcount or 0, unread_count=_unread_count(db, user.id))


@router.post("/{notification_id}/read", response_model=NotificationRead)
def mark_read(notification_id: int, user: AdminUser = Depends(get_current_user), db: Session = Depends(get_db)):
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(notification)
    return notification
