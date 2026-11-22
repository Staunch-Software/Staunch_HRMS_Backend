"""In-app notifications.

Notifying never fails the action that triggered it: the work runs inside a
savepoint and any error is logged and swallowed. Nothing here commits; the
rows are persisted by the caller's own commit.
"""
import logging
from contextlib import contextmanager
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.user import ROLE_HR, AdminUser

logger = logging.getLogger(__name__)

TYPE_ATTENDANCE_SYNC_FAILED = "ATTENDANCE_SYNC_FAILED"


@contextmanager
def guarded(db: Session, what: str):
    """Run the block in a savepoint; on any error roll it back, log, continue."""
    try:
        with db.begin_nested():
            yield
    except Exception:
        logger.exception("Notification skipped (%s)", what)


def hr_user_ids(db: Session) -> list[int]:
    rows = db.query(AdminUser.id).filter(AdminUser.role == ROLE_HR, AdminUser.is_active.is_(True))
    return [row[0] for row in rows.order_by(AdminUser.id)]


def user_id_for_employee(db: Session, employee_id: Optional[int]) -> Optional[int]:
    if employee_id is None:
        return None
    row = (
        db.query(AdminUser.id)
        .filter(AdminUser.employee_id == employee_id, AdminUser.is_active.is_(True))
        .first()
    )
    return row[0] if row else None


def notify_users(
    db: Session, user_ids: Iterable[Optional[int]], type: str, title: str, message: Optional[str] = None,
    link: Optional[str] = None, exclude: Iterable[Optional[int]] = (),
) -> int:
    """Add one notification per distinct user id (None and `exclude` are
    skipped). Returns how many were added; 0 if notifying failed."""
    ids = sorted({uid for uid in user_ids if uid is not None} - set(exclude))
    if not ids:
        return 0
    try:
        with db.begin_nested():
            db.add_all(
                Notification(user_id=uid, type=type, title=title[:200], message=message, link=link)
                for uid in ids
            )
    except Exception:
        logger.exception("Notification skipped (%s)", type)
        return 0
    return len(ids)


def attendance_sync_failed(db: Session, run) -> None:
    with guarded(db, TYPE_ATTENDANCE_SYNC_FAILED):
        window = run.date_from.strftime("%d %b %Y")
        if run.date_to != run.date_from:
            window += f" to {run.date_to.strftime('%d %b %Y')}"
        notify_users(
            db, hr_user_ids(db), TYPE_ATTENDANCE_SYNC_FAILED,
            f"Attendance sync failed ({run.triggered_by})",
            f"Sync for {window} failed: {(run.error_message or 'unknown error')[:300]}",
            "/app/attendance/attendance-log",
        )
