"""A pay group HR builds from scratch with formulas, end to end: create via
the API, employee custom fields on the pay profile, monthly inputs, a payroll
run through the runner (TDS step included), definition versions recorded on
the run, the validate endpoint and the test calculator on unsaved edits.

SITE_X, September 2026, employee on monthly salary 1,98,000, NIGHT shift:
  BASIC     = ROUND(198000 * 26 / 26) = 198000
  SHIFT_PAY = LOOKUP(SHIFT_ALLOWANCE, NIGHT) = 2000 -> gross 2,00,000
  PF_EE     = ROUND(MIN(198000, 15000) * 12%) = 1800 (employer PF the same, cost only)
  TDS       = 11,700 (New regime, 7 months left: projection 14,00,000; see test_tds)
  LOAN 500 (monthly input) -> net = ROUND(200000 - 1800 - 11700 - 500) = 1,86,000
"""
import copy
from datetime import date

from app.db.seed.statutory_seed import seed_tds
from app.models.employee import Employee
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun
from app.models.variable_pay import VariablePayInput

API = "/api/v1"

SITE_X = {
    "standard_days": 26, "net_rounding": "ROUND", "tds_enabled": True, "pf_applicable_default": True,
    "constants": [{"code": "PF_RATE", "label": "PF rate", "value": 0.12},
                  {"code": "PF_CEILING", "label": "PF wage ceiling", "value": 15000},
                  {"code": "OT_RATE", "label": "OT per hour", "value": 50}],
    "lookups": [{"code": "SHIFT_ALLOWANCE", "label": "Shift allowance", "key_label": "Shift",
                 "rows": [{"key": "NIGHT", "value": 2000}, {"key": "DAY", "value": 0}], "default": None}],
    "employee_fields": [{"code": "SHIFT", "label": "Shift", "type": "select", "options": ["DAY", "NIGHT"],
                         "required": True},
                        {"code": "SITE_BONUS", "label": "Site bonus", "type": "number", "default": 0}],
    "lines": [
        {"code": "DAYS", "name": "Days present", "kind": "INFO", "formula": "DAYS_PRESENT", "show_on_payslip": True,
         "statutory_tag": "PAID_DAYS"},
        {"code": "BASIC", "name": "Basic", "kind": "EARNING", "formula": "MONTHLY_SALARY * DAYS / STANDARD_DAYS",
         "rounding": "ROUND", "statutory_tag": "BASIC"},
        {"code": "SHIFT_PAY", "name": "Shift allowance", "kind": "EARNING", "formula": "LOOKUP(SHIFT_ALLOWANCE, SHIFT)",
         "omit_if_zero": True},
        {"code": "SITE_BONUS_PAY", "name": "Site bonus", "kind": "EARNING", "formula": "SITE_BONUS",
         "omit_if_zero": True},
        {"code": "OT", "name": "Overtime", "kind": "EARNING", "formula": "OT_HOURS * OT_RATE", "condition": "OT_HOURS > 0"},
        {"code": "PF_EE", "name": "Employee PF", "kind": "DEDUCTION", "formula": "MIN(BASIC, PF_CEILING) * PF_RATE",
         "rounding": "ROUND", "condition": "PF_APPLICABLE", "statutory_tag": "PF_EE"},
        {"code": "PF_ER", "name": "Employer PF", "kind": "EMPLOYER", "formula": "PF_EE", "condition": "PF_APPLICABLE",
         "statutory_tag": "PF_ER"},
        {"code": "PF_WAGE", "name": "PF wages", "kind": "INFO", "formula": "MIN(BASIC, PF_CEILING)",
         "condition": "PF_APPLICABLE", "statutory_tag": "PF_WAGES"},
    ],
}


def _create(client, code="SITE_X", **extra):
    resp = client.post(f"{API}/pay-groups", json={"code": code, "name": "Site X", "definition": SITE_X, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _employee(db):
    employee = Employee(employee_code="SX-1", first_name="Asha", status="Active", date_of_joining=date(2024, 1, 1))
    db.add(employee)
    db.commit()
    return employee


def _run(client, db, group_id):
    schedule = PaySchedule(schedule_name="Monthly", pay_frequency="Monthly", payment_day_rule={"type": "LastDayOfMonth"},
                           holiday_adjustment="Before", is_active=True, applies_to=None)
    db.add(schedule)
    db.commit()
    resp = client.post(f"{API}/payroll-runs", json={
        "pay_schedule_id": schedule.id, "pay_group_id": group_id, "period_start": "2026-09-01",
        "period_end": "2026-09-30", "pay_date": "2026-09-30"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _calculate(client, run_id):
    resp = client.post(f"{API}/payroll-runs/{run_id}/calculate")
    assert resp.status_code == 200, resp.text
    [item] = client.get(f"{API}/payroll-runs/{run_id}/line-items").json()
    return resp.json(), item, {e["component_code"]: e for e in item["computation_breakdown"]}


def test_custom_group_end_to_end_with_custom_field_tds_and_versions(client, db_session):
    seed_tds(db_session)
    group = _create(client)
    assert (group["calc_model"], group["version"], group["lines_count"]) == ("FORMULA", 1, 8)
    assert group["profile_fields"] == ["monthly_salary", "pf_applicable"]
    assert [f["code"] for f in group["employee_fields"]] == ["SHIFT", "SITE_BONUS"]
    employee = _employee(db_session)

    url = f"{API}/employees/{employee.id}/pay-profile"
    body = {"pay_group_id": group["id"], "monthly_salary": 198000, "effective_from": "2026-01-01"}
    missing = client.put(url, json=body)
    assert missing.status_code == 400 and "SHIFT (Shift) is required" in missing.json()["detail"]
    wrong = client.put(url, json={**body, "extra": {"SHIFT": "EVENING"}})
    assert wrong.status_code == 400 and "must be one of DAY, NIGHT" in wrong.json()["detail"]
    assert client.put(url, json={**body, "extra": {"SHIFT": "NIGHT"}}).status_code == 200

    db_session.add(VariablePayInput(employee_id=employee.id, period_start=date(2026, 9, 1),
                                    period_end=date(2026, 9, 30), present_days=26, lop_days=0, overtime_hours=0,
                                    ad_hoc_earnings=[], ad_hoc_deductions=[{"component_code": "LOAN", "amount": 500}]))
    db_session.commit()
    run_id = _run(client, db_session, group["id"])
    run, item, lines = _calculate(client, run_id)
    assert run["pay_group_definition_version"] == 1
    assert (item["gross_earnings"], item["total_deductions"], item["net_pay"]) == (200000, 14000, 186000)
    assert (item["paid_days"], item["pf_wages"], item["esi_wages"]) == (26, 15000, None)
    assert {c: lines[c]["resolved_amount"] for c in ("BASIC", "SHIFT_PAY", "PF_EE", "PF_ER", "TDS", "LOAN")} == {
        "BASIC": 198000, "SHIFT_PAY": 2000, "PF_EE": 1800, "PF_ER": 1800, "TDS": 11700, "LOAN": 500}
    assert "OT" not in lines and "SITE_BONUS_PAY" not in lines and "PF_WAGE" not in lines
    assert lines["_PAY_GROUP"]["definition_version"] == 1 and lines["PF_EE"]["statutory_tag"] == "PF_EE"

    definition = copy.deepcopy(SITE_X)
    definition["lookups"][0]["rows"][0]["value"] = 3000
    resp = client.put(f"{API}/pay-groups/{group['id']}",
                      json={"definition": definition, "note": "Night shift 3000", "expected_version": 1})
    assert resp.status_code == 200 and resp.json()["version"] == 2
    stale = client.put(f"{API}/pay-groups/{group['id']}", json={"definition": definition, "expected_version": 1})
    assert stale.status_code == 409
    run, item, lines = _calculate(client, run_id)
    assert run["pay_group_definition_version"] == 2 and lines["SHIFT_PAY"]["resolved_amount"] == 3000
    assert db_session.get(PayrollRun, run_id).pay_group_definition_version == 2
    versions = client.get(f"{API}/pay-groups/{group['id']}/versions").json()
    assert [(v["version"], v["note"]) for v in versions] == [(2, "Night shift 3000"), (1, "Created")]
    v1 = client.get(f"{API}/pay-groups/{group['id']}/versions/1").json()
    assert v1["definition"]["lookups"][0]["rows"][0]["value"] == 2000


def test_validate_endpoint_reports_every_error(client):
    bad = copy.deepcopy(SITE_X)
    bad["lines"][1]["formula"] = "MONTHLY_SALARY * DAYZ / "
    bad["lines"][5]["formula"] = "MIN(BASIC, PF_CEILING) * PF_RATE + LATER"
    bad["lines"].append({"code": "LATER", "name": "Later", "kind": "EARNING", "formula": "1"})
    body = client.post(f"{API}/pay-groups/validate", json={"definition": bad}).json()
    assert body["valid"] is False
    assert [(e["line"], e["position"]) for e in body["errors"]] == [("BASIC", 25), ("PF_EE", 36)]
    assert client.post(f"{API}/pay-groups/validate", json={"definition": SITE_X}).json() == {
        "valid": True, "errors": [], "warnings": []}
    resp = client.post(f"{API}/pay-groups", json={"code": "BAD", "name": "Bad", "definition": bad})
    assert resp.status_code == 400 and len(resp.json()["detail"]["errors"]) == 2


def test_test_endpoint_with_unsaved_definition_profile_and_inputs(client, db_session):
    group = _create(client)
    unsaved = copy.deepcopy(SITE_X)
    unsaved["constants"][2]["value"] = 80  # OT_RATE edited, not saved
    body = client.post(f"{API}/pay-groups/{group['id']}/test", json={
        "definition": unsaved, "month": "2026-09",
        "profile": {"monthly_salary": 26000, "pf_applicable": True, "extra": {"SHIFT": "DAY", "SITE_BONUS": 750}},
        "inputs": {"DAYS_PRESENT": 13, "OT_HOURS": 4, "OTHER_PAYMENT": 100, "loan": 50},
    }).json()
    assert body["ok"] is True and body["definition_version"] is None and body["days_in_month"] == 30
    # BASIC 13000 + SITE_BONUS 750 + OT 4 x 80 + OTHER_PAYMENT 100 = 14170; PF 1560; LOAN 50 -> 12560
    assert body["totals"]["gross"] == 14170 and body["totals"]["net_pay"] == 12560
    steps = {s["code"]: s for s in body["lines"]}
    assert steps["OT"]["value"] == 320 and steps["OT"]["variables"] == {"OT_HOURS": 4.0, "OT_RATE": 80.0}
    assert steps["SHIFT_PAY"]["omitted_reason"] == "zero (hidden when zero)"
    assert steps["SHIFT_PAY"]["variables"] == {"SHIFT": "DAY", "SHIFT_ALLOWANCE[DAY]": 0.0}
    assert body["variables"]["SITE_BONUS"] == 750 and body["inputs"]["LOAN"] == 50
    assert [i["component_code"] for i in body["line_items"]][-2:] == ["OTHER_PAYMENT", "LOAN"]

    saved = client.post(f"{API}/pay-groups/{group['id']}/test", json={
        "month": "2026-09", "profile": {"monthly_salary": 26000, "extra": {"SHIFT": "DAY"}},
        "inputs": {"OT_HOURS": 4}}).json()
    assert saved["definition_version"] == 1 and saved["totals"]["gross"] == 26200

    broken = copy.deepcopy(SITE_X)
    broken["lines"][1]["formula"] = "MONTHLY_SALARY / (DAYS - 26)"
    failed = client.post(f"{API}/pay-groups/{group['id']}/test", json={
        "definition": broken, "profile": {"monthly_salary": 1, "extra": {"SHIFT": "DAY"}}}).json()
    assert failed["ok"] is False
    assert (failed["error"]["line"], failed["error"]["position"]) == ("BASIC", 16)
    assert "division by zero" in failed["error"]["message"]
    assert [s["code"] for s in failed["lines"]] == ["DAYS", "BASIC"]
    invalid = client.post(f"{API}/pay-groups/{group['id']}/test", json={"definition": {"lines": []}}).json()
    assert invalid["ok"] is False and invalid["validation"]["errors"]
    bad_input = client.post(f"{API}/pay-groups/{group['id']}/test", json={"inputs": {"BONUS": 1}})
    assert bad_input.status_code == 400 and "unknown input 'BONUS'" in bad_input.json()["detail"]


def test_test_endpoint_uses_employee_profile_and_monthly_input(client, db_session):
    seed_tds(db_session)
    group = _create(client)
    employee = _employee(db_session)
    client.put(f"{API}/employees/{employee.id}/pay-profile", json={
        "pay_group_id": group["id"], "monthly_salary": 198000, "effective_from": "2026-01-01",
        "extra": {"SHIFT": "NIGHT"}})
    db_session.add(VariablePayInput(employee_id=employee.id, period_start=date(2026, 9, 1),
                                    period_end=date(2026, 9, 30), present_days=26, lop_days=0, overtime_hours=0,
                                    ad_hoc_earnings=[], ad_hoc_deductions=[{"component_code": "LOAN", "amount": 500}]))
    db_session.commit()
    body = client.post(f"{API}/pay-groups/{group['id']}/test",
                       json={"employee_id": employee.id, "month": "2026-09"}).json()
    assert body["ok"] and body["profile_source"] == "pay_profile" and body["inputs"]["source"] == "monthly_input"
    assert body["totals"]["net_pay"] == 186000
    assert any(i["component_code"] == "TDS" and i["resolved_amount"] == 11700 for i in body["line_items"])


def test_template_duplicate_and_status(client, db_session):
    group = _create(client)
    copy_resp = client.post(f"{API}/pay-groups", json={"code": "site_y", "name": "Site Y", "template": group["id"]})
    assert copy_resp.status_code == 201 and copy_resp.json()["code"] == "SITE_Y"
    assert copy_resp.json()["definition"]["lines"] == client.get(f"{API}/pay-groups/{group['id']}").json()[
        "definition"]["lines"]
    assert client.post(f"{API}/pay-groups", json={"code": "SITE_Y", "name": "Again"}).status_code == 409
    blank = client.post(f"{API}/pay-groups", json={"code": "BLANK", "name": "Blank"})
    assert blank.status_code == 201 and blank.json()["lines_count"] == 1
    dup = client.post(f"{API}/pay-groups/{group['id']}/duplicate", json={"code": "SITE_Z", "name": "Site Z"})
    assert dup.status_code == 201 and dup.json()["version"] == 1
    note = client.get(f"{API}/pay-groups/{dup.json()['id']}/versions").json()[0]["note"]
    assert note == "Duplicated from SITE_X v1"

    employee = _employee(db_session)
    client.put(f"{API}/employees/{employee.id}/pay-profile", json={
        "pay_group_id": group["id"], "monthly_salary": 1000, "effective_from": "2026-01-01", "extra": {"SHIFT": "DAY"}})
    url = f"{API}/pay-groups/{group['id']}/status"
    blocked = client.patch(url, json={"is_active": False})
    assert blocked.status_code == 409 and blocked.json()["detail"]["employees_count"] == 1
    assert client.patch(url, json={"is_active": False, "force": True}).json()["is_active"] is False
    assert client.patch(url, json={"is_active": True}).json()["is_active"] is True
    assert client.patch(f"{API}/pay-groups/{dup.json()['id']}/status", json={"is_active": False}).status_code == 200


def test_formula_reference(client):
    ref = client.get(f"{API}/pay-groups/formula-reference").json()
    assert {"MONTHLY_SALARY", "DAY_WAGE", "SKILL", "DAYS_PRESENT", "PAID_DAYS", "HAS_INPUT"} <= {
        v["name"] for v in ref["variables"]}
    assert {"OTHER_PAYMENT", "LOAN"} <= {c["code"] for c in ref["input_codes"]}
    assert {"IF", "ROUND", "ROUNDUP", "ROUNDDOWN", "MIN", "MAX", "ABS", "LOOKUP", "CEILING", "FLOOR", "ISBLANK"} <= {
        f["name"] for f in ref["functions"]}
    assert {"PF_EE", "PF_ER", "ESI_EE", "ESI_ER", "PT", "BASIC", "HRA", "PF_WAGES"} <= {
        t["tag"] for t in ref["statutory_tags"]}
    assert ref["line_kinds"] == ["EARNING", "DEDUCTION", "EMPLOYER", "INFO"]
