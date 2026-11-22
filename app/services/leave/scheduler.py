"""In-process monthly leave credit (APScheduler BackgroundScheduler).

Started from the FastAPI lifespan only when LEAVE_CREDIT_ENABLED is true.
Runs credit_month for the current month on the 1st at 00:30 (server local
time): it lapses last month's unused credit for tiers without carry forward
and credits the new month by length of service.
"""
import logging
from datetime import date
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings

logger = logging.getLogger(__name__)

JOB_ID = "leave_monthly_credit"
TRIGGER = {"day": 1, "hour": 0, "minute": 30}

_scheduler: Optional[BackgroundScheduler] = None


def run_monthly_credit(today: Optional[date] = None) -> Optional[dict]:
    from app.core.db import SessionLocal
    from app.services.leave.balance import credit_month

    today = today or date.today()
    db = SessionLocal()
    try:
        stats = credit_month(db, today.year, today.month, as_of=today)
        db.commit()
        logger.info(
            "Monthly leave credit %s: %s credit(s), %s lapse(s), %s carry forward(s); missing joining date: %s",
            stats["ref"], stats["credit_entries"], stats["lapse_entries"], stats["carry_forward_entries"],
            ", ".join(stats["missing_joining_date"]) or "none",
        )
        return stats
    except Exception:
        db.rollback()
        logger.exception("Monthly leave credit crashed")
        return None
    finally:
        db.close()


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        run_monthly_credit,
        CronTrigger(**TRIGGER),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=6 * 3600,
    )
    return scheduler


def start_leave_scheduler() -> Optional[BackgroundScheduler]:
    global _scheduler
    if not settings.LEAVE_CREDIT_ENABLED or _scheduler is not None:
        return _scheduler
    scheduler = build_scheduler()
    scheduler.start()
    _scheduler = scheduler
    logger.info("Monthly leave credit scheduled on the 1st at 00:30")
    return scheduler


def shutdown_leave_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def is_running() -> bool:
    return _scheduler is not None
