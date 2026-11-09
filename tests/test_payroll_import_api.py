"""Payroll workbook import: preview -> commit, on a synthetic two-sheet workbook.

CCTV (seeded params), Skilled, wage 1000, 24 days, loan 500 -> net 26677
(same figures as test_pay_group_run). Tuticorin, per-day salary 1492.3
(base 1000 + 492.3 allowance) x 20 days = 29846, flat PF 3000 -> net 26846.
"""
import io
from datetime import date

import openpyxl
import pytest

from app.db.seed.pay_groups_seed import seed_pay_groups
from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun
from app.models.variable_pay import VariablePayInput
from app.services.payroll_engine.pay_group_runner import resolve_pay_profile

PREVIEW = "/api/v1/payroll/import/excel/preview"
COMMIT = "/api/v1/payroll/import/excel/commit"
CCTV_SHEET = "Oct CCTV"
TUTI_SHEET = "Oct Tuticorin"


def _workbook(cctv_wage=1000) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = CCTV_SHEET
    ws["A1"] = "CCTV wages"
    for col, header in {"A": "Sl.No.", "B": "Name", "C": "ST-EMP ID", "D": "Wage", "E": "Skill Set",
                        "F": "Present days", "U": "Net"}.items():
        ws[f"{col}2"] = header
    ws.append([1, "T.BRELIK", "ST-EMP-11", cctv_wage, "Skilled", 24])
    ws["S3"], ws["U3"] = 500, 26677
    ws.append([2, "R.CAZIMIR", None, 1000, "Skilled", 26])
    ws["U4"] = 29416

    tu = wb.create_sheet(TUTI_SHEET)
    for col, header in {"A": "", "B": "NAME", "D": "Per Day Salary", "E": "Days", "K": "Net"}.items():
        tu[f"{col}3"] = header
    tu["A4"] = "S.No"  # repeated header row: not a data row
    tu.append([1, "ELSPETH.M - SUPERVISOR", None, 1492.3, 20])
    tu["K5"] = 26846
    tu.append([2, "DOVRAN", None, 1492.3, 20])
    tu.append([3, "XANTHOS QUILL", None, 1492.3, 20])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _employee(db, code, first, last):
    e = Employee(employee_code=code, first_name=first, last_name=last, status="Active", date_of_joining=date(2025, 1, 1))
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


@pytest.fixture()
def people(db_session):
    seed_pay_groups(db_session)
    return {
        "brelik": _employee(db_session, "STEMP0011", "Brelik", "T"),
        "cazimir": _employee(db_session, "STEMP0012", "Cazimir", "R"),
        "elspeth": _employee(db_session, "STEMP0013", "Elspeth", "M"),
        "dovran_k": _employee(db_session, "STEMP0014", "Dovran", "K"),
        "dovran_s": _employee(db_session, "STEMP0015", "Dovran", "S"),
    }


def _preview(client, content=None, period="2026-10"):
    files = {"file": ("payroll.xlsx", content or _workbook(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return client.post(PREVIEW, files=files, data={"period": period})


def _rows(body):
    return {(s["sheet"], r["row_number"]): r for s in body["sheets"] for r in s["rows"]}


def test_preview_shape_and_match_statuses(client, people):
    resp = _preview(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_id"] and body["period"] == "2026-10"
    assert [(s["sheet"], s["pay_group_code"]) for s in body["sheets"]] == [(TUTI_SHEET, "TUTICORIN"), (CCTV_SHEET, "CCTV")]
    rows = _rows(body)
    assert len(rows) == 5  # the repeated header row is not a data row

    by_id = rows[(CCTV_SHEET, 3)]
    assert by_id["match_status"] == "MATCHED_BY_ID"
    assert by_id["employee_code"] == "STEMP0011"
    assert by_id["profile"]["daily_wage"] == 1000 and by_id["profile"]["skill_level"] == "Skilled"
    assert by_id["inputs"]["present_days"] == 24
    assert by_id["inputs"]["ad_hoc_deductions"] == [{"component_code": "LOAN", "amount": 500.0}]
    assert by_id["excel_net_pay"] == 26677 and by_id["computed_net_pay"] == 26677

    assert rows[(CCTV_SHEET, 4)]["match_status"] == "MATCHED_BY_NAME"
    assert rows[(CCTV_SHEET, 4)]["employee_id"] == people["cazimir"].id
    supervisor = rows[(TUTI_SHEET, 5)]
    assert supervisor["match_status"] == "MATCHED_BY_NAME" and supervisor["score"] >= 0.85
    assert supervisor["profile"]["daily_wage"] == pytest.approx(1000)
    assert supervisor["computed_net_pay"] == 26846
    ambiguous = rows[(TUTI_SHEET, 6)]
    assert ambiguous["match_status"] == "AMBIGUOUS" and ambiguous["employee_id"] is None
    assert {c["employee_id"] for c in ambiguous["candidates"]} == {people["dovran_k"].id, people["dovran_s"].id}
    assert rows[(TUTI_SHEET, 7)]["match_status"] == "UNMATCHED"

    summary = body["summary"]
    assert summary["total"] == {"MATCHED_BY_ID": 1, "MATCHED_BY_NAME": 2, "AMBIGUOUS": 1, "UNMATCHED": 1, "rows": 5}
    assert summary["sheets"][TUTI_SHEET]["AMBIGUOUS"] == 1


def test_preview_rejects_bad_period_and_bad_file(client, people):
    assert _preview(client, period="2026-13").status_code == 400
    assert _preview(client, content=b"not a workbook").status_code == 400


def test_commit_creates_profiles_inputs_and_runs_then_reuses_them(client, db_session, people):
    batch = _preview(client).json()["batch_id"]
    resp = client.post(COMMIT, json={"batch_id": batch})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["committed"] == 3
    assert body["profiles_created"] == 3 and body["inputs_created"] == 3
    assert sorted(s["reason"] for s in body["skipped"]) == ["needs review (ambiguous)", "needs review (unmatched)"]
    runs = {r["pay_group_code"]: r for r in body["runs"]}
    assert set(runs) == {"CCTV", "TUTICORIN"} and all(r["created"] for r in runs.values())
    assert runs["TUTICORIN"]["employees"] == 1

    schedule = db_session.query(PaySchedule).one()
    assert schedule.schedule_name == "Monthly (Pay Groups)" and schedule.payment_day_rule == {"type": "LastDayOfMonth"}
    run = db_session.get(PayrollRun, runs["CCTV"]["run_id"])
    assert run.status == "Draft" and run.pay_date == date(2026, 10, 31)
    assert run.pay_group_id == db_session.query(PayGroup).filter_by(code="CCTV").one().id

    profile = resolve_pay_profile(db_session, people["brelik"].id, date(2026, 10, 15))
    assert profile.effective_from == date(2026, 10, 1) and float(profile.daily_wage) == 1000
    vi = db_session.query(VariablePayInput).filter_by(employee_id=people["brelik"].id).one()
    assert vi.source == "ExcelImport" and vi.import_batch_id == batch and float(vi.present_days) == 24
    assert vi.ad_hoc_deductions[0]["component_code"] == "LOAN"

    calc = client.post(f"/api/v1/payroll-runs/{run.id}/calculate")
    assert calc.status_code == 200 and calc.json()["status"] == "Calculated"
    item = client.get(f"/api/v1/payroll-runs/{run.id}/line-items/{people['brelik'].id}").json()
    assert item["net_pay"] == 26677

    again = client.post(COMMIT, json={"batch_id": _preview(client).json()["batch_id"]}).json()
    assert again["profiles_created"] == 0 and again["profiles_unchanged"] == 3
    assert again["inputs_created"] == 0 and again["inputs_updated"] == 3
    assert {r["pay_group_code"]: r["run_id"] for r in again["runs"]} == {k: v["run_id"] for k, v in runs.items()}
    assert not any(r["created"] for r in again["runs"])
    assert db_session.get(PayrollRun, run.id).status == "Draft"  # inputs changed -> recalculate
    assert db_session.query(EmployeePayProfile).count() == 3
    assert db_session.query(VariablePayInput).count() == 3
    assert db_session.query(PayrollRun).count() == 2


def test_changed_profile_creates_a_new_effective_dated_row(client, db_session, people):
    client.post(COMMIT, json={"batch_id": _preview(client, period="2026-09").json()["batch_id"]})
    body = client.post(COMMIT, json={
        "batch_id": _preview(client, content=_workbook(cctv_wage=1100), period="2026-10").json()["batch_id"]}).json()
    assert body["profiles_created"] == 1 and body["profiles_unchanged"] == 2
    profiles = (db_session.query(EmployeePayProfile).filter_by(employee_id=people["brelik"].id)
                .order_by(EmployeePayProfile.effective_from).all())
    assert [(p.effective_from, p.effective_to) for p in profiles] == [
        (date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), None)]
    assert float(resolve_pay_profile(db_session, people["brelik"].id, date(2026, 9, 30)).daily_wage) == 1000
    assert float(resolve_pay_profile(db_session, people["brelik"].id, date(2026, 10, 31)).daily_wage) == 1100


def test_decisions_resolve_and_skip_rows(client, db_session, people):
    batch = _preview(client).json()["batch_id"]
    resp = client.post(COMMIT, json={"batch_id": batch, "decisions": [
        {"sheet": TUTI_SHEET, "row_number": 6, "employee_id": people["dovran_s"].id},
        {"sheet": CCTV_SHEET, "row_number": 4, "skip": True},
    ]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    committed = {r["employee_code"] for r in body["rows"]}
    assert committed == {"STEMP0011", "STEMP0013", "STEMP0015"}
    reasons = {(s["sheet"], s["row_number"]): s["reason"] for s in body["skipped"]}
    assert reasons == {(CCTV_SHEET, 4): "skipped by reviewer", (TUTI_SHEET, 7): "needs review (unmatched)"}
    assert resolve_pay_profile(db_session, people["cazimir"].id, date(2026, 10, 31)) is None


def test_commit_rejects_one_employee_on_two_rows(client, people):
    batch = _preview(client).json()["batch_id"]
    resp = client.post(COMMIT, json={"batch_id": batch, "decisions": [
        {"sheet": TUTI_SHEET, "row_number": 6, "employee_id": people["brelik"].id}]})
    assert resp.status_code == 400
    assert "one row only" in resp.json()["detail"]


def test_commit_refuses_locked_period(client, db_session, people):
    db_session.add(VariablePayInput(employee_id=people["brelik"].id, period_start=date(2026, 10, 1),
                                    period_end=date(2026, 10, 31), present_days=20, lop_days=0, overtime_hours=0,
                                    ad_hoc_earnings=[], ad_hoc_deductions=[], status="Locked"))
    db_session.commit()
    resp = client.post(COMMIT, json={"batch_id": _preview(client).json()["batch_id"]})
    assert resp.status_code == 409
    assert "Locked" in resp.json()["detail"]
    assert db_session.query(EmployeePayProfile).count() == 0
    assert db_session.query(PayrollRun).count() == 0


def test_commit_unknown_batch_and_bad_decision(client, people):
    assert client.post(COMMIT, json={"batch_id": "nope"}).status_code == 404
    batch = _preview(client).json()["batch_id"]
    bad = client.post(COMMIT, json={"batch_id": batch, "decisions": [{"sheet": TUTI_SHEET, "row_number": 6}]})
    assert bad.status_code == 400


def test_import_requires_auth(client, people):
    resp = client.post(COMMIT, json={"batch_id": "x"}, headers={"Authorization": ""})
    assert resp.status_code == 401
