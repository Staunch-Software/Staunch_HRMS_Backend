"""In-process daily attendance sync (APScheduler BackgroundScheduler).

Started from the FastAPI lifespan only when ATTENDANCE_SYNC_ENABLED is true.
Runs at ATTENDANCE_SYNC_TIME (HH:MM, server local time) and syncs
today - ATTENDANCE_SYNC_LOOKBACK_DAYS .. today.
"""
import logging
from datetime import date, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings

logger = logging.getLogger(__name__)

JOB_ID = "attendance_daily_sync"

_scheduler: Optional[BackgroundScheduler] = None


def parse_sync_time(value: str) -> tuple[int, int]:
    try:
        hour_str, minute_str = value.strip().split(":")
        hour, minute = int(hour_str), int(minute_str)
    except ValueError as exc:
        raise ValueError(f"ATTENDANCE_SYNC_TIME must be HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"ATTENDANCE_SYNC_TIME out of range: {value!r}")
    return hour, minute


def run_scheduled_sync() -> None:
    from app.core.db import SessionLocal
    from app.services.attendance.sync import sync_attendance

    today = date.today()
    date_from = today - timedelta(days=max(settings.ATTENDANCE_SYNC_LOOKBACK_DAYS, 0))
    db = SessionLocal()
    try:
        run = sync_attendance(db, date_from, today, triggered_by="scheduler")
        logger.info(
            "Scheduled attendance sync %s: %s (fetched=%s new=%s days=%s)",
            run.id, run.status, run.records_fetched, run.records_new, run.days_recomputed,
        )
    except Exception:
        logger.exception("Scheduled attendance sync crashed")
    finally:
        db.close()


def start_scheduler() -> Optional[BackgroundScheduler]:
    global _scheduler
    if not settings.ATTENDANCE_SYNC_ENABLED or _scheduler is not None:
        return _scheduler
    hour, minute = parse_sync_time(settings.ATTENDANCE_SYNC_TIME)
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        run_scheduled_sync,
        CronTrigger(hour=hour, minute=minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Attendance sync scheduled daily at %02d:%02d (lookback %s day(s))",
                hour, minute, settings.ATTENDANCE_SYNC_LOOKBACK_DAYS)
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
