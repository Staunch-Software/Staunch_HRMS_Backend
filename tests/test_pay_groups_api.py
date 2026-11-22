"""Pay-group API, pay-profile effective-dating and payroll-run integration.
Synthetic employees only; expected figures hand-calculated in comments.
"""
from datetime import date

from app.db.seed.pay_groups_seed import seed_pay_groups
from app.models.employee import Employee
from app.models.pay_group import PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.variable_pay import VariablePayInput

PERIOD_START = date(2026, 9, 1)
PERIOD_END = date(2026, 9, 30)


def _groups(db_session) -> dict[str, PayGroup]:
    seed_pay_groups(db_session)
    return {g.code: g for g in db_session.query(PayGroup).all()}


def _employee(db_session, code, status="Active") -> Employee:
    employee = Employee(employee_code=code, first_name=code, status=status, date_of_joining=date(2025, 1, 1))
    db_session.add(employee)
    db_session.commit()
    db_session.refresh(employee)
    return employee


def _set_profile(client, employee_id, **body):
    body.setdefault("effective_from", "2026-01-01")
    return client.put(f"/api/v1/employees/{employee_id}/pay-profile", json=body)


def test_pay_group_list_get_and_update(client, db_session):
    groups = _groups(db_session)
    resp = client.get("/api/v1/pay-groups")
    assert resp.status_code == 200
    codes = {g["code"] for g in resp.json()}
    assert codes == {"CHENNAI_HO", "TUTICORIN", "KKNPP_HARI", "MCBS", "CCTV", "ESFAS", "BHAVNAGAR"}

    assert all(g["calc_model"] == "FORMULA" and g["version"] == 1 and g["lines_count"] > 0 for g in resp.json())
    assert all(g["definition"] is None for g in resp.json())

    cctv = client.get(f"/api/v1/pay-groups/{groups['CCTV'].id}").json()
    assert cctv["calc_model"] == "FORMULA" and cctv["param_spec"] == {}
    assert cctv["params"]["pf_threshold"] == 25000
    assert cctv["profile_fields"] == ["daily_wage", "skill_level", "pf_applicable", "fixed_monthly_amount"]
    assert cctv["skill_levels"] == ["HIGHLY_SKILLED", "SEMI_SKILLED", "SKILLED"]

    definition = cctv["definition"]
    allowance = next(lk for lk in definition["lookups"] if lk["code"] == "ALLOWANCE_PER_DAY")
    next(row for row in allowance["rows"] if row["key"] == "SKILLED")["value"] = 150
    resp = client.put(f"/api/v1/pay-groups/{groups['CCTV'].id}",
                      json={"definition": definition, "name": "CCTV site", "note": "Allowance revised"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "CCTV site" and body["version"] == 2 and body["definition"]["version"] == 2
    allowance = next(lk for lk in body["definition"]["lookups"] if lk["code"] == "ALLOWANCE_PER_DAY")
    assert {r["key"]: r["value"] for r in allowance["rows"]}["SKILLED"] == 150
    versions = client.get(f"/api/v1/pay-groups/{groups['CCTV'].id}/versions").json()
    assert [(v["version"], v["note"]) for v in versions] == [(2, "Allowance revised"),
                                                            (1, "Created from the DAILY_WAGE_ATTENDANCE model")]
    assert versions[0]["changed_by"] == "admin"


def test_pay_group_update_rejects_bad_definition_and_params(client, db_session):
    groups = _groups(db_session)
    definition = dict(groups["MCBS"].definition)
    definition["lines"] = [*definition["lines"], {"code": "EXTRA", "name": "Extra", "kind": "EARNING",
                                                  "formula": "WAGES * BONUS_RAT"}]
    resp = client.put(f"/api/v1/pay-groups/{groups['MCBS'].id}", json={"definition": definition})
    assert resp.status_code == 400
    [error] = resp.json()["detail"]["errors"]
    assert error["line"] == "EXTRA" and error["position"] == 9
    assert "unknown variable 'BONUS_RAT' (did you mean BONUS_RATE?)" in error["message"]
    resp = client.put(f"/api/v1/pay-groups/{groups['MCBS'].id}", json={"params": dict(groups["MCBS"].params)})
    assert resp.status_code == 400 and "edit its definition" in resp.json()["detail"]
    assert client.get("/api/v1/pay-groups/9999").status_code == 404

    # A group still on a legacy strategy keeps its params editing and validation.
    params = dict(groups["MCBS"].params)
    legacy = PayGroup(code="LEGACY", name="Legacy", calc_model="DAILY_WAGE_ATTENDANCE", params=params, is_active=True)
    db_session.add(legacy)
    db_session.commit()
    bad = {k: v for k, v in params.items() if k != "pf_threshold"}
    bad["bonus_rate"] = "x"
    resp = client.put(f"/api/v1/pay-groups/{legacy.id}", json={"params": bad})
    assert resp.status_code == 400
    assert "missing keys: pf_threshold" in resp.json()["detail"]
    assert "'bonus_rate' must be a non-negative number" in resp.json()["detail"]


def test_pay_groups_require_auth(client):
    assert client.get("/api/v1/pay-groups", headers={"Authorization": ""}).status_code == 401


def test_pay_profile_effective_dating(client, db_session):
    groups = _groups(db_session)
    employee = _employee(db_session, "E-PP1")

    first = _set_profile(client, employee.id, pay_group_id=groups["MCBS"].id, daily_wage=1000,
                         skill_level="Skilled", effective_from="2026-01-01")
    assert first.status_code == 200, first.text
    second = _set_profile(client, employee.id, pay_group_id=groups["CCTV"].id, daily_wage=1100,
                          skill_level="Skilled", effective_from="2026-09-01")
    assert second.status_code == 200, second.text
    assert second.json()["pay_group_code"] == "CCTV"

    history = client.get(f"/api/v1/employees/{employee.id}/pay-profiles").json()
    assert [h["pay_group_code"] for h in history] == ["CCTV", "MCBS"]
    assert history[1]["effective_to"] == "2026-08-31"
    assert history[0]["effective_to"] is None

    as_of_aug = client.get(f"/api/v1/employees/{employee.id}/pay-profile", params={"as_of": "2026-08-15"})
    assert as_of_aug.json()["pay_group_code"] == "MCBS"
    current = client.get(f"/api/v1/employees/{employee.id}/pay-profile", params={"as_of": "2026-09-15"})
    assert current.json()["daily_wage"] == 1100

    backdated = _set_profile(client, employee.id, pay_group_id=groups["CCTV"].id, daily_wage=1, skill_level="Skilled",
                             effective_from="2026-08-01")
    assert backdated.status_code == 400


def test_pay_profile_validation(client, db_session):
    groups = _groups(db_session)
    employee = _employee(db_session, "E-PP2")
    resp = _set_profile(client, employee.id, pay_group_id=groups["CCTV"].id, skill_level="Skilled")
    assert resp.status_code == 400
    assert "daily_wage or fixed_monthly_amount is required" in resp.json()["detail"]
    resp = _set_profile(client, employee.id, pay_group_id=groups["BHAVNAGAR"].id, daily_wage=900, skill_level="Expert")
    assert resp.status_code == 400
    assert "skill level 'EXPERT'" in resp.json()["detail"]
    assert client.get(f"/api/v1/employees/{employee.id}/pay-profile").status_code == 404
    assert _set_profile(client, 9999, pay_group_id=groups["CCTV"].id, daily_wage=1).status_code == 404
