"""Which days the Matrix sync has fully covered, and the catch-up that fills
the gaps.

Matrix filters on server receive time, so a sync for day D fetches what
Matrix received on D. D is fully covered by a successful run whose range
includes D and which finished after D ended (server local time); a run
during D itself is partial. The nightly run at ATTENDANCE_SYNC_TIME covers
up to the day before it ran, so in normal operation covered_through is the
day before yesterday and the sync is stale only when it falls behind that.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.attendance import AttendanceSyncRun
from app.services.attendance.sync import MAX_SYNC_DAYS, sync_attendance

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_RUNNING = "running"
MAX_CATCHUP_DAYS = 62
# The nightly run covers everything before the day it runs on.
EXPECTED_LAG_DAYS = 2


def to_local(value: Optional[datetime]) -> Optional[datetime]:
    """Aware (or naive UTC, as SQLite returns it) -> naive server local time,
    the same clock punch event times are stored in."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().replace(tzinfo=None)


def covered_span(run: AttendanceSyncRun) -> Optional[tuple[date, date]]:
    """Days the run fully covered, or None."""
    finished = to_local(run.finished_at)
    if run.status != STATUS_SUCCESS or finished is None:
        return None
    end = min(run.date_to, finished.date() - timedelta(days=1))
    return (run.date_from, end) if end >= run.date_from else None


def _covered_days(db: Session, start: date, end: date) -> set[date]:
    runs = db.scalars(
        select(AttendanceSyncRun).where(
            AttendanceSyncRun.status == STATUS_SUCCESS,
            AttendanceSyncRun.finished_at.is_not(None),
            AttendanceSyncRun.date_to >= start,
            AttendanceSyncRun.date_from <= end,
        )
    ).all()
    days: set[date] = set()
    for run in runs:
        span = covered_span(run)
        if span is None:
            continue
        d, last = max(span[0], start), min(span[1], end)
        while d <= last:
            days.add(d)
            d += timedelta(days=1)
    return days


def first_synced_day(db: Session) -> Optional[date]:
    """The earliest day any successful sync fetched: days before it predate
    the sync and are not judged."""
    return db.scalar(
        select(func.min(AttendanceSyncRun.date_from)).where(AttendanceSyncRun.status == STATUS_SUCCESS)
    )


def uncovered_dates(db: Session, first: date, last: date, today: Optional[date] = None) -> list[date]:
    """Past days in [first, last] (before today) that no successful sync has
    fully covered, from the first synced day on (none before any sync)."""
    today = today or date.today()
    origin = first_synced_day(db)
    if origin is None:
        return []
    start, end = max(first, origin), min(last, today - timedelta(days=1))
    if end < start:
        return []
    covered = _covered_days(db, start, end)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if start + timedelta(days=i) not in covered]


@dataclass
class CoverageStatus:
    today: date
    covered_through: Optional[date]
    expected_through: date
    stale: bool
    last_success_at: Optional[datetime]
    last_run_status: Optional[str]
    last_run_at: Optional[datetime]
    running: bool
    missing_from: Optional[date]
    sync_from: date
    sync_to: date
    missing_dates: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "today": self.today,
            "covered_through": self.covered_through,
            "expected_through": self.expected_through,
            "stale": self.stale,
            "last_success_at": self.last_success_at,
            "last_run_status": self.last_run_status,
            "last_run_at": self.last_run_at,
            "running": self.running,
            "missing_from": self.missing_from,
            "missing_dates": list(self.missing_dates),
            "sync_from": self.sync_from,
            "sync_to": self.sync_to,
            "max_sync_days": MAX_SYNC_DAYS,
        }


def coverage_status(db: Session, today: Optional[date] = None) -> CoverageStatus:
    """covered_through: the day before the earliest uncovered day of the last
    MAX_CATCHUP_DAYS (from the first synced day on), else yesterday. stale:
    covered_through is behind the day before yesterday (last night's sync
    did not complete), or nothing was ever synced. sync_from..sync_to is the
    catch-up range: covered_through - 1 (late uploads) .. today, at most
    MAX_CATCHUP_DAYS back."""
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    floor = today - timedelta(days=MAX_CATCHUP_DAYS - 1)
    origin = first_synced_day(db)
    expected = today - timedelta(days=EXPECTED_LAG_DAYS)

    missing: list[date] = []
    if origin is None:
        covered_through = None
    else:
        start = max(floor, origin)
        missing = uncovered_dates(db, start, yesterday, today)
        covered_through = (missing[0] - timedelta(days=1)) if missing else yesterday

    if covered_through is None:
        lookback = max(settings.ATTENDANCE_SYNC_LOOKBACK_DAYS, 0)
        sync_from = today - timedelta(days=lookback)
    else:
        sync_from = max(covered_through - timedelta(days=1), floor)
    sync_from = min(sync_from, today)

    last_success = db.scalar(
        select(func.max(AttendanceSyncRun.finished_at)).where(AttendanceSyncRun.status == STATUS_SUCCESS)
    )
    last_run = db.scalars(select(AttendanceSyncRun).order_by(AttendanceSyncRun.id.desc()).limit(1)).first()
    running = db.scalar(
        select(func.count(AttendanceSyncRun.id)).where(
            AttendanceSyncRun.status == STATUS_RUNNING,
            AttendanceSyncRun.started_at >= datetime.now(timezone.utc) - timedelta(hours=1),
        )
    ) or 0

    return CoverageStatus(
        today=today,
        covered_through=covered_through,
        expected_through=expected,
        stale=covered_through is None or covered_through < expected,
        last_success_at=last_success,
        last_run_status=last_run.status if last_run else None,
        last_run_at=(last_run.finished_at or last_run.started_at) if last_run else None,
        running=running > 0,
        missing_from=missing[0] if missing else None,
        missing_dates=missing,
        sync_from=sync_from,
        sync_to=today,
    )


def chunk_range(date_from: date, date_to: date, size: int = MAX_SYNC_DAYS) -> list[tuple[date, date]]:
    chunks = []
    start = date_from
    while start <= date_to:
        end = min(start + timedelta(days=size - 1), date_to)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def catch_up(
    db: Session, triggered_by: str = "catchup", today: Optional[date] = None, client=None,
) -> list[AttendanceSyncRun]:
    """Syncs sync_from..today in chunks of MAX_SYNC_DAYS. Stops at the first
    failed chunk (later chunks would leave a hole behind them)."""
    status = coverage_status(db, today)
    runs: list[AttendanceSyncRun] = []
    for date_from, date_to in chunk_range(status.sync_from, status.sync_to):
        run = sync_attendance(db, date_from, date_to, triggered_by=triggered_by, client=client)
        runs.append(run)
        if run.status != STATUS_SUCCESS:
            break
    return runs
