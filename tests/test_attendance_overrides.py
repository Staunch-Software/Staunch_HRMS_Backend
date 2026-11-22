"""Manual day corrections, the daily endpoint's override merge and the
effective_days contract monthly processing reads."""
from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.attendance import AttendanceDaily, AttendanceOverride
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.services.attendance.effective import effective_days
from app.services.attendance.overrides import KIND_STATUS, KIND_TIMES, build_values, save_override

D3 = date(2026, 11, 3)
D4 = date(2026, 11, 4)
D5 = date(2026, 11, 5)
DAILY = "/api/v1/attendance/daily"


def _employee(db, code, first, last):
    e = Employee(employee_code=code, first_name=first, last_name=last, status="Active")
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def _matrix_day(db, code, day, punch_in, punch_out=None, employee_id=None):
    p_in = datetime.combine(day, datetime.strptime(punch_in, "%H:%M").time())
    p_out = datetime.combine(day, datetime.strptime(punch_out, "%H:%M").time()) if punch_out else None
    row = AttendanceDaily(
        employee_code=code, employee_id=employee_id, work_date=day, punch_in=p_in, punch_in_source="IN",
        punch_out=p_out, punch_out_source="OUT" if p_out else None,
        worked_minutes=int((p_out - p_in).total_seconds() // 60) if p_out else None,
        punch_count=2 if p_out else 1, username="device user",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def people(db_session):
    db_session.add(LeaveType(code="CL", name="Casual Leave"))
    db_session.commit()
    return {
        "arlo": _employee(db_session, "STEMP0101", "Arlo", "Venn"),
        "brisa": _employee(db_session, "STEMP0102", "Brisa", "Tolland"),
    }


def _put(client, employee_id, day, **body):
    return client.put(f"{DAILY}/{employee_id}/{day.isoformat()}", json=body)


def _daily(client, **params):
    resp = client.get(DAILY, params={"date_from": "2026-11-01", "date_to": "2026-11-30", **params})
    assert resp.status_code == 200, resp.text
    return {(r["employee_code"], r["work_date"]): r for r in resp.json()}


def test_manual_edit_one_active_and_revert(client, db_session, people):
    arlo = people["arlo"]
    _matrix_day(db_session, "STEMP0101", D3, "09:05")

    resp = _put(client, arlo.id, D3, kind="TIMES", punch_out="18:00", reason="Forgot to punch out")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "MANUAL" and body["created_by"] == "admin" and body["is_active"] is True
    assert body["punch_in"] is None and body["punch_out"] == "2026-11-03T18:00:00"

    row = _daily(client)[("STEMP0101", "2026-11-03")]
    assert row["punch_in"] == "2026-11-03T09:05:00" and row["punch_in_source"] == "IN"
    assert row["punch_out"] == "2026-11-03T18:00:00" and row["punch_out_source"] == "OVERRIDE"
    assert row["worked_minutes"] == 535 and row["is_single_punch"] is False
    assert row["matrix_punch_out"] is None and row["override"]["reason"] == "Forgot to punch out"
    assert row["is_override_only"] is False and row["id"] is not None

    # Same values again: no new row.
    assert _put(client, arlo.id, D3, kind="TIMES", punch_out="18:00", reason="Forgot to punch out").status_code == 200
    assert db_session.query(AttendanceOverride).count() == 1

    second = _put(client, arlo.id, D3, kind="STATUS", status_code="OD", reason="Client visit")
    assert second.status_code == 200
    active = db_session.query(AttendanceOverride).filter(AttendanceOverride.is_active.is_(True)).all()
    assert [(o.kind, o.status_code) for o in active] == [("STATUS", "OD")]

    history = client.get("/api/v1/attendance/overrides", params={"employee_id": arlo.id}).json()
    assert [(h["kind"], h["is_active"]) for h in history] == [("STATUS", True), ("TIMES", False)]
    assert history[1]["deactivated_by"] == "admin" and history[1]["deactivated_at"] is not None

    row = _daily(client)[("STEMP0101", "2026-11-03")]
    assert row["override"]["status_code"] == "OD" and row["punch_out"] is None  # STATUS keeps Matrix times

    reverted = client.delete(f"{DAILY}/{arlo.id}/2026-11-03")
    assert reverted.status_code == 200 and reverted.json()["is_active"] is False
    row = _daily(client)[("STEMP0101", "2026-11-03")]
    assert row["override"] is None and row["punch_out"] is None and row["is_single_punch"] is True
    assert client.delete(f"{DAILY}/{arlo.id}/2026-11-03").status_code == 404
    assert client.get("/api/v1/attendance/overrides", params={"employee_id": arlo.id, "active_only": True}).json() == []


def test_manual_edit_validation(client, db_session, people):
    arlo = people["arlo"]
    assert _put(client, arlo.id, D3, kind="TIMES", punch_in="09:00").status_code == 422
    assert _put(client, arlo.id, D3, kind="TIMES", punch_in="09:00", reason="  ").status_code == 400
    assert _put(client, arlo.id, D3, kind="TIMES", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="TIMES", punch_in="18:00", punch_out="09:00", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="TIMES", punch_in="7 o'clock", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="TIMES", punch_in="2026-11-04T09:00:00", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="STATUS", status_code="ZZ", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="STATUS", status_code="LEAVE", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="STATUS", status_code="P", leave_type_code="CL", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="STATUS", status_code="A", half="MIDDLE", reason="x").status_code == 400
    assert _put(client, arlo.id, D3, kind="WHATEVER", reason="x").status_code == 400
    assert _put(client, 9999, D3, kind="STATUS", status_code="P", reason="x").status_code == 404
    assert db_session.query(AttendanceOverride).count() == 0

    leave = _put(client, arlo.id, D3, kind="STATUS", status_code="cl", half="FIRST_HALF", reason="Half-day CL")
    assert leave.status_code == 200
    assert (leave.json()["status_code"], leave.json()["leave_type_code"], leave.json()["half"]) == ("LEAVE", "CL", "FIRST_HALF")
    hd = _put(client, arlo.id, D4, kind="STATUS", status_code="HD", leave_type_code="CL", reason="HD + CL")
    assert hd.status_code == 200 and hd.json()["leave_type_code"] == "CL"
    iso = _put(client, arlo.id, D5, kind="TIMES", punch_in="2026-11-05T08:30:00", punch_out="5:30 PM", reason="x")
    assert iso.status_code == 200 and iso.json()["punch_out"] == "2026-11-05T17:30:00"


def test_daily_endpoint_merges_overrides_and_override_only_days(client, db_session, people):
    arlo, brisa = people["arlo"], people["brisa"]
    _matrix_day(db_session, "STEMP0101", D3, "09:00", "18:00")
    _matrix_day(db_session, "STEMP0999", D3, "10:00")  # unmatched device user
    assert _put(client, arlo.id, D3, kind="TIMES", punch_in="08:30", reason="Gate reader down").status_code == 200
    assert _put(client, brisa.id, D4, kind="STATUS", status_code="CL", reason="Leave").status_code == 200
    assert _put(client, brisa.id, D5, kind="TIMES", punch_in="09:00", punch_out="17:00", reason="Site").status_code == 200

    rows = _daily(client)
    assert list(rows) == [("STEMP0102", "2026-11-05"), ("STEMP0102", "2026-11-04"),
                          ("STEMP0101", "2026-11-03"), ("STEMP0999", "2026-11-03")]
    arlo_row = rows[("STEMP0101", "2026-11-03")]
    assert arlo_row["punch_in"] == "2026-11-03T08:30:00" and arlo_row["matrix_punch_in"] == "2026-11-03T09:00:00"
    assert arlo_row["punch_out"] == "2026-11-03T18:00:00" and arlo_row["worked_minutes"] == 570

    leave_day = rows[("STEMP0102", "2026-11-04")]
    assert leave_day["is_override_only"] is True and leave_day["id"] is None and leave_day["punch_count"] == 0
    assert leave_day["employee_id"] == brisa.id and leave_day["employee_name"] == "Brisa Tolland"
    assert leave_day["override"]["status_code"] == "LEAVE" and leave_day["override"]["leave_type_code"] == "CL"
    assert leave_day["is_single_punch"] is False and leave_day["punch_in"] is None
    site_day = rows[("STEMP0102", "2026-11-05")]
    assert site_day["worked_minutes"] == 480 and site_day["punch_in_source"] == "OVERRIDE"
    assert rows[("STEMP0999", "2026-11-03")]["override"] is None

    only_brisa = _daily(client, employee_code="ST-EMP-102")
    assert set(only_brisa) == {("STEMP0102", "2026-11-04"), ("STEMP0102", "2026-11-05")}
    assert _daily(client, employee_code="STEMP0999") == {("STEMP0999", "2026-11-03"): rows[("STEMP0999", "2026-11-03")]}


def _values(**kwargs):
    return build_values(**kwargs)


def test_effective_days_precedence(db_session, people):
    arlo, brisa = people["arlo"], people["brisa"]
    _matrix_day(db_session, "STEMP0101", D3, "09:00", "18:00")
    _matrix_day(db_session, "STEMP0101", D4, "09:10")
    _matrix_day(db_session, "OLDCODE01", D5, "09:00", "17:00", employee_id=brisa.id)  # cached link only
    save_override(db_session, arlo.id, arlo.employee_code, D3,
                  _values(kind=KIND_STATUS, work_date=D3, status_code="A", reason="Absent"),
                  source="MANUAL", created_by="hr")
    save_override(db_session, arlo.id, arlo.employee_code, D5,
                  _values(kind=KIND_TIMES, work_date=D5, punch_in=datetime(2026, 11, 5, 9, 30).time()),
                  source="UPLOAD_DAILY", created_by="hr")
    old, _ = save_override(db_session, brisa.id, brisa.employee_code, D4,
                           _values(kind=KIND_STATUS, work_date=D4, status_code="WO"), source="MANUAL", created_by="hr")
    db_session.commit()
    old.is_active = False
    db_session.commit()

    days = effective_days(db_session, [arlo.id, brisa.id], date(2026, 11, 1), date(2026, 11, 30))
    assert set(days) == {(arlo.id, D3), (arlo.id, D4), (arlo.id, D5), (brisa.id, D5)}

    absent = days[(arlo.id, D3)]
    assert (absent.source, absent.kind, absent.status_code) == ("OVERRIDE", "STATUS", "A")
    assert absent.punch_in == datetime(2026, 11, 3, 9, 0) and absent.worked_minutes == 540
    assert absent.missing_out is False and absent.punch_count == 2 and absent.override_source == "MANUAL"

    single = days[(arlo.id, D4)]
    assert (single.source, single.kind, single.status_code) == ("MATRIX", "TIMES", None)
    assert single.missing_out is True and single.worked_minutes is None and single.half == "FULL"

    override_only = days[(arlo.id, D5)]
    assert override_only.source == "OVERRIDE" and override_only.punch_count == 0
    assert override_only.punch_in == datetime(2026, 11, 5, 9, 30) and override_only.missing_out is True

    cached = days[(brisa.id, D5)]
    assert cached.source == "MATRIX" and cached.worked_minutes == 480

    only_brisa = effective_days(db_session, [brisa.id], date(2026, 11, 1), date(2026, 11, 30))
    assert set(only_brisa) == {(brisa.id, D5)}
    assert effective_days(db_session, [], date(2026, 11, 1), date(2026, 11, 30)) == {}
    assert set(effective_days(db_session, None, D3, D3)) == {(arlo.id, D3)}


def test_save_override_is_idempotent_and_db_allows_one_active(db_session, people):
    arlo = people["arlo"]
    values = _values(kind=KIND_STATUS, work_date=D3, status_code="P", reason="r")
    first, outcome = save_override(db_session, arlo.id, "ST-EMP-101", D3, values, source="MANUAL", created_by="hr")
    db_session.commit()
    assert outcome == "created" and first.employee_code == "STEMP0101"
    _, outcome = save_override(db_session, arlo.id, arlo.employee_code, D3, dict(values), source="MANUAL", created_by="hr")
    assert outcome == "unchanged"
    _, outcome = save_override(db_session, arlo.id, arlo.employee_code, D3, {**values, "status_code": "A"},
                               source="MANUAL", created_by="hr")
    db_session.commit()
    assert outcome == "replaced" and db_session.query(AttendanceOverride).count() == 2

    db_session.add(AttendanceOverride(employee_id=arlo.id, employee_code=arlo.employee_code, work_date=D3,
                                      kind="STATUS", status_code="P", source="MANUAL", is_active=True))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
