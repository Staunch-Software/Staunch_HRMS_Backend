"""Matrix attendance sync: fetch -> dedupe raw punches -> rebuild daily rows.

Idempotent: punches are keyed on (employee_code, event_datetime,
entry_exit_type, device_name) and AttendanceDaily rows are recomputed from
all stored punches of each touched (employee_code, work_date), so running
the same window twice yields identical data.
"""
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceDaily, AttendancePunch, AttendanceSyncRun
from app.models.employee import Employee
from app.models.org import Department
from app.services.attendance.matrix_client import MatrixAPIError, MatrixClient, MatrixPunch, parse_record
from app.services.employee_code import normalize_employee_code

logger = logging.getLogger(__name__)

IN = "0"
OUT = "1"
SOURCE_IN = "IN"
SOURCE_OUT = "OUT"
SOURCE_FALLBACK_FIRST = "FALLBACK_FIRST_PUNCH"
SOURCE_FALLBACK_LAST = "FALLBACK_LAST_PUNCH"

MAX_SYNC_DAYS = 31

# One sync at a time per process (scheduler + manual button could overlap).
_sync_lock = threading.Lock()


def compute_daily(punches: Iterable[tuple[datetime, str]]) -> dict:
    """Apply the first-IN / last-OUT rules to one employee-day's punches,
    given as (event_datetime, entry_exit_type) pairs."""
    ordered = sorted(punches, key=lambda p: p[0])
    result = {
        "punch_in": None,
        "punch_in_source": None,
        "punch_out": None,
        "punch_out_source": None,
        "worked_minutes": None,
        "punch_count": len(ordered),
    }
    if not ordered:
        return result

    ins = [ts for ts, kind in ordered if kind == IN]
    outs = [ts for ts, kind in ordered if kind == OUT]

    if ins:
        result["punch_in"], result["punch_in_source"] = ins[0], SOURCE_IN
    else:
        result["punch_in"], result["punch_in_source"] = ordered[0][0], SOURCE_FALLBACK_FIRST

    if len(ordered) > 1:
        if outs:
            result["punch_out"], result["punch_out_source"] = outs[-1], SOURCE_OUT
        else:
            result["punch_out"], result["punch_out_source"] = ordered[-1][0], SOURCE_FALLBACK_LAST

    if result["punch_in"] and result["punch_out"] and result["punch_out"] > result["punch_in"]:
        result["worked_minutes"] = int((result["punch_out"] - result["punch_in"]).total_seconds() // 60)
    return result


@dataclass(frozen=True)
class EmployeeRef:
    id: int
    name: Optional[str]
    department: Optional[str]


def employee_code_map(db: Session) -> dict[str, EmployeeRef]:
    """normalized employee_code -> EmployeeRef (lowest id wins on a collision).
    Selects explicit columns only, so attendance keeps working even when the
    Employee model has columns not yet migrated into the database."""
    stmt = (
        select(Employee.id, Employee.employee_code, Employee.first_name, Employee.last_name, Department.name)
        .outerjoin(Department, Department.id == Employee.department_id)
        .order_by(Employee.id.desc())
    )
    mapping: dict[str, EmployeeRef] = {}
    for emp_id, code, first_name, last_name, department in db.execute(stmt):
        name = " ".join(part for part in (first_name, last_name) if part) or None
        mapping[normalize_employee_code(code)] = EmployeeRef(emp_id, name, department)
    return mapping


def relink_employees(db: Session) -> int:
    """Refresh the cached employee_id on punches/daily rows from the current
    employee master. Returns the number of employee codes whose link changed.
    Does not commit."""
    id_by_code = {code: emp.id for code, emp in employee_code_map(db).items()}
    changed = 0
    current = defaultdict(set)
    for code, emp_id in db.execute(select(AttendanceDaily.employee_code, AttendanceDaily.employee_id).distinct()):
        current[code].add(emp_id)
    for code, emp_id in db.execute(select(AttendancePunch.employee_code, AttendancePunch.employee_id).distinct()):
        current[code].add(emp_id)
    for code, linked_ids in current.items():
        target = id_by_code.get(code)
        if linked_ids == {target}:
            continue
        db.execute(update(AttendancePunch).where(AttendancePunch.employee_code == code).values(employee_id=target))
        db.execute(update(AttendanceDaily).where(AttendanceDaily.employee_code == code).values(employee_id=target))
        changed += 1
    return changed


def _store_punches(db: Session, punches: list[MatrixPunch], id_by_code: dict[str, int]) -> int:
    if not punches:
        return 0
    lo = min(p.event_datetime for p in punches)
    hi = max(p.event_datetime for p in punches)
    existing = set(
        db.execute(
            select(
                AttendancePunch.employee_code,
                AttendancePunch.event_datetime,
                AttendancePunch.entry_exit_type,
                AttendancePunch.device_name,
            ).where(AttendancePunch.event_datetime >= lo, AttendancePunch.event_datetime <= hi)
        ).all()
    )
    new = 0
    for p in punches:
        key = (p.employee_code, p.event_datetime, p.entry_exit_type, p.device_name)
        if key in existing:
            continue
        existing.add(key)
        db.add(
            AttendancePunch(
                employee_code=p.employee_code,
                raw_userid=p.raw_userid,
                username=p.username,
                event_datetime=p.event_datetime,
                event_date=p.event_date,
                entry_exit_type=p.entry_exit_type,
                device_name=p.device_name,
                received_at=p.received_at,
                employee_id=id_by_code.get(p.employee_code),
            )
        )
        new += 1
    db.flush()
    return new


def recompute_daily(db: Session, keys: Iterable[tuple[str, date]], id_by_code: dict[str, int]) -> int:
    """Rebuild AttendanceDaily for each (employee_code, work_date) from all
    stored punches. Does not commit."""
    count = 0
    for code, work_date in sorted(set(keys)):
        rows = db.scalars(
            select(AttendancePunch)
            .where(AttendancePunch.employee_code == code, AttendancePunch.event_date == work_date)
            .order_by(AttendancePunch.event_datetime, AttendancePunch.id)
        ).all()
        daily = db.scalar(
            select(AttendanceDaily).where(
                AttendanceDaily.employee_code == code, AttendanceDaily.work_date == work_date
            )
        )
        if not rows:
            if daily is not None:
                db.delete(daily)
            continue
        values = compute_daily((r.event_datetime, r.entry_exit_type) for r in rows)
        username = next((r.username for r in reversed(rows) if r.username), None)
        if daily is None:
            daily = AttendanceDaily(employee_code=code, work_date=work_date)
            db.add(daily)
        daily.employee_id = id_by_code.get(code)
        daily.username = username
        for field, value in values.items():
            setattr(daily, field, value)
        count += 1
    db.flush()
    return count


def _date_range(date_from: date, date_to: date) -> list[date]:
    return [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]


def sync_attendance(
    db: Session,
    date_from: date,
    date_to: date,
    triggered_by: str = "manual",
    client: Optional[MatrixClient] = None,
) -> AttendanceSyncRun:
    """Fetch every day in [date_from, date_to] (by Matrix receive date), store
    new punches and rebuild the daily rows they touch. Always returns the
    AttendanceSyncRun; a failure is recorded on it (status="failed") rather
    than raised, and leaves punches/daily rows untouched."""
    if date_to < date_from:
        raise ValueError("date_to must be on or after date_from")
    client = client or MatrixClient()

    with _sync_lock:
        run = AttendanceSyncRun(
            triggered_by=triggered_by, date_from=date_from, date_to=date_to, status="running"
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

        try:
            relink_employees(db)
            id_by_code = {code: emp.id for code, emp in employee_code_map(db).items()}

            punches: list[MatrixPunch] = []
            fetched = 0
            skipped: list[str] = []
            for day in _date_range(date_from, date_to):
                for record in client.fetch_day_raw(day):
                    fetched += 1
                    try:
                        punches.append(parse_record(record))
                    except (ValueError, TypeError) as exc:
                        skipped.append(f"{record.get('userid') if isinstance(record, dict) else record!r}: {exc}")

            new = _store_punches(db, punches, id_by_code)
            days = recompute_daily(db, {(p.employee_code, p.event_date) for p in punches}, id_by_code)

            run.records_fetched = fetched
            run.records_new = new
            run.days_recomputed = days
            run.status = "success"
            if skipped:
                run.error_message = f"Skipped {len(skipped)} unparseable record(s): " + "; ".join(skipped[:10])
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            db.rollback()
            run = db.get(AttendanceSyncRun, run_id)
            run.status = "failed"
            run.error_message = str(exc)[:2000] if isinstance(exc, MatrixAPIError) else f"{exc.__class__.__name__}: {exc}"[:2000]
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            logger.warning("Attendance sync %s failed: %s", run_id, run.error_message)
        db.refresh(run)
        return run
