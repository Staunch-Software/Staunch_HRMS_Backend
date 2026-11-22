"""Attendance uploads (daily corrections, monthly muster, site monthly days):
preview -> decisions -> commit, on synthetic workbooks."""
import io
from datetime import date, datetime, time
from decimal import Decimal

import openpyxl
import pytest

from app.db.seed.pay_groups_seed import seed_pay_groups
from app.models.attendance import AttendanceOverride
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.variable_pay import VariablePayInput
from app.services.attendance import imports as imports_module
from app.services.attendance.overrides import parse_date_value, parse_time_value

BASE = "/api/v1/attendance/import"
MONTH = "2026-11"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DAILY_HEADERS = ["Employee ID", "Employee Name", "Date", "In Time", "Out Time", "Reason"]
SITE_HEADERS = ["Employee ID", "Employee Name", "Days Present", "OT Days", "OT Hours", "LOP Days"]


def _xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _employee(db, code, first, last):
    e = Employee(employee_code=code, first_name=first, last_name=last, status="Active", date_of_joining=date(2025, 1, 1))
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def _profile(db, employee, group_code):
    group = db.query(PayGroup).filter(PayGroup.code == group_code).one()
    db.add(EmployeePayProfile(employee_id=employee.id, pay_group_id=group.id, daily_wage=900,
                              effective_from=date(2026, 1, 1), extra={}))
    db.commit()


@pytest.fixture()
def people(db_session):
    seed_pay_groups(db_session)
    return {
        "arlo": _employee(db_session, "STEMP0101", "Arlo", "Venn"),
        "brisa": _employee(db_session, "STEMP0102", "Brisa", "Tolland"),
        "corin_a": _employee(db_session, "STEMP0103", "Corin", "A"),
        "corin_b": _employee(db_session, "STEMP0104", "Corin", "B"),
    }


@pytest.fixture()
def leave_types(db_session):
    db_session.add_all([LeaveType(code="CL", name="Casual Leave"), LeaveType(code="SL", name="Sick Leave"),
                        LeaveType(code="OLD", name="Retired leave", is_active=False)])
    db_session.commit()


def _preview(client, kind, content, month=MONTH):
    return client.post(f"{BASE}/{kind}/preview", files={"file": ("upload.xlsx", content, XLSX)}, data={"month": month})


def _ok_preview(client, kind, content, month=MONTH):
    resp = _preview(client, kind, content, month)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _commit(client, kind, batch_id, decisions=()):
    return client.post(f"{BASE}/{kind}/commit", json={"batch_id": batch_id, "decisions": list(decisions)})


def _ok_commit(client, kind, batch_id, decisions=()):
    resp = _commit(client, kind, batch_id, decisions)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(body):
    return {r["row_number"]: r for r in body["rows"]}


def _active(db, employee_id=None):
    q = db.query(AttendanceOverride).filter(AttendanceOverride.is_active.is_(True))
    if employee_id is not None:
        q = q.filter(AttendanceOverride.employee_id == employee_id)
    return {o.work_date: o for o in q.all()}


# --- parsing helpers -----------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("09:30", time(9, 30)), ("9:05", time(9, 5)), ("9.30", time(9, 30)), ("18:45:10", time(18, 45, 10)),
    ("6:15 PM", time(18, 15)), ("6 pm", time(18, 0)), ("12:10 AM", time(0, 10)), ("12:00 P.M.", time(12, 0)),
    (time(8, 0), time(8, 0)), (datetime(1899, 12, 30, 7, 45), time(7, 45)), (0.5, time(12, 0)), (None, None), ("", None),
])
def test_parse_time_formats(raw, expected):
    assert parse_time_value(raw) == expected


@pytest.mark.parametrize("raw", ["25:00", "9", "13 PM", "nine", 9.5, "9:75"])
def test_parse_time_rejects(raw):
    with pytest.raises(ValueError):
        parse_time_value(raw)


@pytest.mark.parametrize("raw, expected", [
    ("2026-11-03", date(2026, 11, 3)), ("03-11-2026", date(2026, 11, 3)), ("03/11/2026", date(2026, 11, 3)),
    ("3-Nov-2026", date(2026, 11, 3)), (datetime(2026, 11, 3, 0, 0), date(2026, 11, 3)), (46329, date(2026, 11, 3)),
])
def test_parse_date_formats(raw, expected):
    assert parse_date_value(raw) == expected


# --- DAILY ---------------------------------------------------------------------------------

def test_daily_happy_path_time_formats_and_idempotent_recommit(client, db_session, people):
    content = _xlsx([
        DAILY_HEADERS,
        ["STEMP0101", "Arlo Venn", date(2026, 11, 3), time(9, 0), time(18, 30), "Forgot to punch"],
        ["ST-EMP-102", "Brisa Tolland", "04-11-2026", "9:15 AM", "6:45 PM", None],
        [101, "Arlo Venn", datetime(2026, 11, 5), 0.375, "17:00", ""],
        ["STEMP0102", None, "2026-11-06", None, "18:00", None],
    ])
    body = _ok_preview(client, "daily", content)
    assert body["kind"] == "DAILY" and body["month"] == MONTH
    assert body["summary"]["total"] == 4 and body["summary"]["errors"] == 0
    assert body["summary"]["matched_by_id"] == 3  # unknown ID 101 -> matched by name
    rows = _rows(body)
    assert rows[2]["employee"]["code"] == "STEMP0101"
    assert rows[3]["data"] == {"date": "2026-11-04", "in_time": "09:15", "out_time": "18:45",
                               "worked_minutes": 570, "reason": None}
    assert rows[4]["match_status"] == "MATCHED_BY_NAME" and rows[4]["data"]["in_time"] == "09:00"
    assert rows[5]["data"]["in_time"] is None and rows[5]["data"]["out_time"] == "18:00"

    result = _ok_commit(client, "daily", body["batch_id"])
    assert result["committed_rows"] == 4 and result["overrides_created"] == 4 and result["skipped"] == []

    arlo = _active(db_session, people["arlo"].id)
    first = arlo[date(2026, 11, 3)]
    assert first.kind == "TIMES" and first.source == "UPLOAD_DAILY" and first.created_by == "admin"
    assert first.punch_in == datetime(2026, 11, 3, 9, 0) and first.punch_out == datetime(2026, 11, 3, 18, 30)
    assert first.reason == "Forgot to punch" and first.import_batch_id == body["batch_id"]
    brisa = _active(db_session, people["brisa"].id)
    assert brisa[date(2026, 11, 4)].reason == "Daily corrections upload"
    assert brisa[date(2026, 11, 6)].punch_in is None and brisa[date(2026, 11, 6)].punch_out.hour == 18

    again = _ok_commit(client, "daily", _ok_preview(client, "daily", content)["batch_id"])
    assert again["overrides_unchanged"] == 4 and again["overrides_created"] == 0
    assert db_session.query(AttendanceOverride).count() == 4

    # The batch is consumed by a commit.
    assert _commit(client, "daily", body["batch_id"]).status_code == 404


def test_daily_validation_errors(client, db_session, people):
    content = _xlsx([
        DAILY_HEADERS,
        ["STEMP0101", "Arlo Venn", "2026-12-01", "09:00", "18:00", None],
        ["STEMP0101", "Arlo Venn", "2026-11-02", "18:00", "09:00", None],
        ["STEMP0101", "Arlo Venn", "2026-11-03", None, None, "no times"],
        ["STEMP0101", "Arlo Venn", "2026-11-04", "25:00", "18:00", None],
        ["STEMP0101", "Arlo Venn", "not a date", "09:00", "18:00", None],
        ["STEMP0101", "Arlo Venn", None, "09:00", "18:00", None],
    ])
    body = _ok_preview(client, "daily", content)
    rows = _rows(body)
    assert "outside 2026-11" in rows[2]["errors"][0]
    assert "not after In Time" in rows[3]["errors"][0]
    assert rows[4]["errors"] == ["In Time and Out Time are both blank."]
    assert rows[5]["errors"][0].startswith("In Time:")
    assert rows[6]["errors"][0].startswith("Date:")
    assert rows[7]["errors"] == ["Date is required."]
    assert body["summary"]["errors"] == 6

    result = _ok_commit(client, "daily", body["batch_id"])
    assert result["committed_rows"] == 0 and len(result["skipped"]) == 6
    assert all(s["reason"].startswith("row has errors") for s in result["skipped"])
    assert db_session.query(AttendanceOverride).count() == 0


def test_daily_duplicate_day_is_flagged(client, db_session, people):
    content = _xlsx([
        DAILY_HEADERS,
        ["STEMP0101", "Arlo Venn", "2026-11-03", "09:00", "18:00", None],
        ["STEMP0101", "Arlo Venn", "2026-11-03", "10:00", "19:00", None],
    ])
    body = _ok_preview(client, "daily", content)
    rows = _rows(body)
    assert rows[2]["errors"] == [] and "also in row 2" in rows[3]["errors"][0]
    assert rows[3]["employee_errors"] == rows[3]["errors"]
    result = _ok_commit(client, "daily", body["batch_id"])
    assert result["overrides_created"] == 1 and result["skipped"][0]["row_number"] == 3


def test_daily_name_matching_and_decisions(client, db_session, people):
    content = _xlsx([
        DAILY_HEADERS,
        [None, "Arlo Venn", "2026-11-03", "09:00", "18:00", None],
        [None, "Corin", "2026-11-03", "09:00", "18:00", None],
        [None, "Zed Unknownperson", "2026-11-03", "09:00", "18:00", None],
        [None, "Corin", "2026-11-04", "09:00", "18:00", None],
    ])
    body = _ok_preview(client, "daily", content)
    rows = _rows(body)
    assert rows[2]["match_status"] == "MATCHED_BY_NAME"
    assert rows[3]["match_status"] == "AMBIGUOUS"
    assert {c["employee_id"] for c in rows[3]["candidates"]} == {people["corin_a"].id, people["corin_b"].id}
    assert rows[4]["match_status"] == "UNMATCHED" and rows[4]["employee"] is None
    summary = body["summary"]
    assert (summary["matched_by_name"], summary["ambiguous"], summary["unmatched"]) == (1, 2, 1)

    assert _commit(client, "daily", body["batch_id"], [{"row_number": 99, "skip": True}]).status_code == 400
    assert _commit(client, "daily", body["batch_id"], [{"row_number": 3}]).status_code == 400

    result = _ok_commit(client, "daily", body["batch_id"], [
        {"row_number": 3, "employee_id": people["corin_b"].id},
        {"row_number": 4, "skip": True},
    ])
    assert result["committed_rows"] == 2
    reasons = {s["row_number"]: s["reason"] for s in result["skipped"]}
    assert reasons == {4: "skipped by reviewer", 5: "needs review (ambiguous)"}
    assert set(_active(db_session, people["corin_b"].id)) == {date(2026, 11, 3)}
    assert _active(db_session, people["corin_a"].id) == {}


def test_bad_kind_month_and_file(client, people):
    content = _xlsx([DAILY_HEADERS, ["STEMP0101", "Arlo Venn", "2026-11-03", "09:00", "18:00", None]])
    assert _preview(client, "weekly", content).status_code == 404
    assert _preview(client, "daily", content, month="2026-13").status_code == 400
    assert _preview(client, "daily", b"not an xlsx").status_code == 400
    missing = _preview(client, "daily", _xlsx([["Employee ID", "Employee Name", "Date"], ["STEMP0101", "A", "x"]]))
    assert missing.status_code == 400 and "In Time" in missing.json()["detail"]
    assert _preview(client, "daily", _xlsx([DAILY_HEADERS])).status_code == 400


# --- MUSTER --------------------------------------------------------------------------------

def _muster(rows, days=30, header_for=None):
    header = ["Employee ID", "Employee Name"] + [(header_for(d) if header_for else d) for d in range(1, days + 1)]
    weekdays = [None, None] + [date(2026, 11, d).strftime("%a") if d <= 30 else None for d in range(1, days + 1)]
    out = [header, weekdays]
    for emp_id, name, codes in rows:
        line = [emp_id, name] + [None] * days
        for day, code in codes.items():
            line[1 + day] = code
        out.append(line)
    return _xlsx(out)


def test_muster_happy_path_and_idempotent_recommit(client, db_session, people, leave_types):
    codes = {1: "P", 2: "a", 3: "HD", 4: "CL", 5: "HD/SL", 6: "WO", 7: "H", 8: "OD"}
    content = _muster([("STEMP0101", "Arlo Venn", codes), ("STEMP0102", "Brisa Tolland", {})])
    body = _ok_preview(client, "muster", content)
    assert body["summary"]["total"] == 1 and body["summary"]["blank_rows"] == 1
    assert body["warnings"] == []
    row = body["rows"][0]
    assert row["errors"] == [] and row["employee"]["id"] == people["arlo"].id
    assert row["data"]["days"] == {"1": "P", "2": "A", "3": "HD", "4": "CL", "5": "HD/SL", "6": "WO", "7": "H",
                                   "8": "OD"}
    assert row["data"]["counts"]["LEAVE"] == 1 and row["data"]["counts"]["HD"] == 2

    result = _ok_commit(client, "muster", body["batch_id"])
    assert result["overrides_created"] == 8
    days = _active(db_session, people["arlo"].id)
    assert days[date(2026, 11, 2)].status_code == "A" and days[date(2026, 11, 2)].kind == "STATUS"
    assert (days[date(2026, 11, 4)].status_code, days[date(2026, 11, 4)].leave_type_code) == ("LEAVE", "CL")
    assert (days[date(2026, 11, 5)].status_code, days[date(2026, 11, 5)].leave_type_code) == ("HD", "SL")
    assert days[date(2026, 11, 8)].source == "UPLOAD_MUSTER"

    again = _ok_commit(client, "muster", _ok_preview(client, "muster", content)["batch_id"])
    assert again["overrides_unchanged"] == 8 and again["overrides_created"] == 0

    changed = _muster([("STEMP0101", "Arlo Venn", {**codes, 2: "P"})])
    third = _ok_commit(client, "muster", _ok_preview(client, "muster", changed)["batch_id"])
    assert third["overrides_replaced"] == 1 and third["overrides_unchanged"] == 7
    history = db_session.query(AttendanceOverride).filter(AttendanceOverride.work_date == date(2026, 11, 2)).all()
    assert sorted((o.status_code, o.is_active) for o in history) == [("A", False), ("P", True)]


def test_muster_code_validation(client, db_session, people, leave_types):
    content = _muster([
        ("STEMP0101", "Arlo Venn", {1: "P", 2: "XY", 3: "OLD", 31: "P"}),
        ("STEMP0102", "Brisa Tolland", {1: "P", 2: "HD/ZZ"}),
    ], days=31)
    body = _ok_preview(client, "muster", content)
    rows = {r["employee"]["code"]: r for r in body["rows"]}
    arlo_errors = " | ".join(rows["STEMP0101"]["errors"])
    assert "Day 2: 'XY' is not a day code" in arlo_errors
    assert "Day 3:" in arlo_errors  # inactive leave type
    assert "Day 31: 2026-11 has only 30 days" in arlo_errors
    assert "Day 2:" in rows["STEMP0102"]["errors"][0]
    result = _ok_commit(client, "muster", body["batch_id"])
    assert result["committed_rows"] == 0 and db_session.query(AttendanceOverride).count() == 0


def test_muster_accepts_any_code_without_leave_types(client, db_session, people, monkeypatch):
    monkeypatch.setattr(imports_module, "active_leave_codes", lambda db: None)
    content = _muster([("STEMP0101", "Arlo Venn", {1: "CL", 2: "TOOLONG", 3: "P"})])
    body = _ok_preview(client, "muster", content)
    row = body["rows"][0]
    assert any("TOOLONG" in e for e in row["errors"])
    assert row["data"]["days"] == {"1": "CL", "3": "P"}
    assert any("accepted without checking" in w for w in row["warnings"])
    assert "CL" in body["warnings"][0]


def test_muster_date_headers(client, db_session, people, leave_types):
    content = _muster([("STEMP0101", "Arlo Venn", {1: "P", 15: "SL"})],
                      header_for=lambda d: datetime(2026, 11, d))
    body = _ok_preview(client, "muster", content)
    assert body["rows"][0]["data"]["days"] == {"1": "P", "15": "SL"}

    wrong = _muster([("STEMP0101", "Arlo Venn", {1: "P"})], days=2, header_for=lambda d: datetime(2026, 10, d))
    resp = _preview(client, "muster", wrong)
    assert resp.status_code == 400 and "not a day of 2026-11" in resp.json()["detail"]


def test_muster_duplicate_employee_needs_decision(client, db_session, people, leave_types):
    content = _muster([("STEMP0101", "Arlo Venn", {1: "P"}), ("STEMP0101", "Arlo Venn", {2: "P"})])
    body = _ok_preview(client, "muster", content)
    statuses = [r["match_status"] for r in body["rows"]]
    assert statuses == ["MATCHED_BY_ID", "AMBIGUOUS"]
    clash = _commit(client, "muster", body["batch_id"], [{"row_number": 4, "employee_id": people["arlo"].id}])
    assert clash.status_code == 400 and "one row only" in clash.json()["detail"]
    assert db_session.query(AttendanceOverride).count() == 0


# --- SITE_MONTHLY --------------------------------------------------------------------------

def test_site_monthly_upsert_keeps_other_fields(client, db_session, people):
    _profile(db_session, people["arlo"], "TUTICORIN")
    _profile(db_session, people["brisa"], "MCBS")
    earning = {"component_code": "BONUS", "amount": 500, "remarks": "festival"}
    db_session.add(VariablePayInput(employee_id=people["arlo"].id, period_start=date(2026, 11, 1),
                                    period_end=date(2026, 11, 30), present_days=20, overtime_hours=5,
                                    ad_hoc_earnings=[earning], ad_hoc_deductions=[], remarks="manual"))
    db_session.commit()
    content = _xlsx([
        SITE_HEADERS,
        ["STEMP0101", "Arlo Venn", 24, None, None, 2],
        ["STEMP0102", "Brisa Tolland", 26.5, 1, 6, None],
    ])
    body = _ok_preview(client, "site-monthly", content)
    rows = {r["employee"]["code"]: r for r in body["rows"]}
    assert rows["STEMP0101"]["data"]["pay_group"] == "TUTICORIN" and rows["STEMP0101"]["errors"] == []
    result = _ok_commit(client, "site-monthly", body["batch_id"])
    assert (result["inputs_created"], result["inputs_updated"]) == (1, 1)

    db_session.expire_all()
    arlo = db_session.query(VariablePayInput).filter_by(employee_id=people["arlo"].id).one()
    assert arlo.present_days == Decimal("24") and arlo.lop_days == Decimal("2")
    assert arlo.overtime_hours == Decimal("5") and arlo.ad_hoc_earnings == [earning] and arlo.remarks == "manual"
    assert arlo.source == "AttendanceUpload" and arlo.import_batch_id == body["batch_id"]
    brisa = db_session.query(VariablePayInput).filter_by(employee_id=people["brisa"].id).one()
    assert (brisa.present_days, brisa.overtime_days, brisa.overtime_hours) == (Decimal("26.5"), Decimal("1"), Decimal("6"))
    assert brisa.status == "Draft"

    again = _ok_commit(client, "site-monthly", _ok_preview(client, "site-monthly", content)["batch_id"])
    assert again["inputs_unchanged"] == 2 and again["inputs_updated"] == 0


def test_site_monthly_validation_chennai_warning_and_missing_profile(client, db_session, people):
    _profile(db_session, people["arlo"], "TUTICORIN")
    _profile(db_session, people["corin_a"], "CHENNAI_HO")
    content = _xlsx([
        SITE_HEADERS,
        ["STEMP0101", "Arlo Venn", 31, None, -1, None],
        ["STEMP0103", "Corin A", 22, None, None, None],
        ["STEMP0104", "Corin B", 22, None, None, None],
        ["STEMP0102", "Brisa Tolland", None, 2, None, None],
    ])
    body = _ok_preview(client, "site-monthly", content)
    rows = {r["employee"]["code"]: r for r in body["rows"]}
    assert any("between 0 and 30" in e for e in rows["STEMP0101"]["errors"])
    assert any("cannot be negative" in e for e in rows["STEMP0101"]["errors"])
    assert rows["STEMP0103"]["errors"] == [] and "CHENNAI_HO" in rows["STEMP0103"]["warnings"][0]
    assert "No pay profile" in rows["STEMP0104"]["errors"][0]
    assert rows["STEMP0102"]["errors"][0] == "Days Present is required."

    result = _ok_commit(client, "site-monthly", body["batch_id"])
    assert result["committed_rows"] == 1 and result["rows"][0]["employee_code"] == "STEMP0103"


def test_site_monthly_locked(client, db_session, people):
    _profile(db_session, people["arlo"], "TUTICORIN")
    content = _xlsx([SITE_HEADERS, ["STEMP0101", "Arlo Venn", 24, None, None, None]])
    body = _ok_preview(client, "site-monthly", content)
    assert body["rows"][0]["errors"] == []

    db_session.add(VariablePayInput(employee_id=people["arlo"].id, period_start=date(2026, 11, 1),
                                    period_end=date(2026, 11, 30), present_days=20, status="Locked",
                                    ad_hoc_earnings=[], ad_hoc_deductions=[]))
    db_session.commit()
    resp = _commit(client, "site-monthly", body["batch_id"])
    assert resp.status_code == 409 and "Locked" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.query(VariablePayInput).one().present_days == Decimal("20")

    locked_preview = _ok_preview(client, "site-monthly", content)
    assert "Locked" in locked_preview["rows"][0]["errors"][0]


# --- templates -----------------------------------------------------------------------------

def _template(client, kind, month=MONTH):
    resp = client.get(f"{BASE}/{kind}/template?month={month}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(XLSX)
    return openpyxl.load_workbook(io.BytesIO(resp.content))


def test_templates(client, db_session, people):
    _profile(db_session, people["arlo"], "TUTICORIN")
    _profile(db_session, people["corin_a"], "CHENNAI_HO")

    daily = _template(client, "daily")
    assert [c.value for c in daily.active[1]] == DAILY_HEADERS and daily.sheetnames[1] == "Instructions"

    muster = _template(client, "muster").active
    header = [c.value for c in muster[1]]
    assert header[:2] == ["Employee ID", "Employee Name"] and header[2:] == list(range(1, 31))
    assert muster.cell(row=2, column=3).value == date(2026, 11, 1).strftime("%a")
    assert [muster.cell(row=r, column=1).value for r in range(3, 7)] == ["STEMP0101", "STEMP0102", "STEMP0103", "STEMP0104"]

    site = _template(client, "site-monthly").active
    assert [c.value for c in site[1]][:6] == SITE_HEADERS
    assert [(site.cell(row=r, column=1).value, site.cell(row=r, column=7).value) for r in range(2, site.max_row + 1)] == [
        ("STEMP0101", "TUTICORIN")]

    # An untouched muster template has nothing to import.
    buf = io.BytesIO()
    _template(client, "muster").save(buf)
    resp = _preview(client, "muster", buf.getvalue())
    assert resp.status_code == 400 and "No attendance data" in resp.json()["detail"]

    assert client.get(f"{BASE}/muster/template?month=2026-1x").status_code == 400
    assert client.get(f"{BASE}/nope/template?month={MONTH}").status_code == 404


def test_import_requires_hr(client):
    resp = client.get(f"{BASE}/daily/template?month={MONTH}", headers={"Authorization": ""})
    assert resp.status_code == 401
