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

    cctv = client.get(f"/api/v1/pay-groups/{groups['CCTV'].id}").json()
    assert cctv["calc_model"] == "DAILY_WAGE_ATTENDANCE"
    assert cctv["param_spec"]["pf_threshold"]["type"] == "number"

    params = dict(cctv["params"])
    params["allowance_per_day_by_skill"] = {**params["allowance_per_day_by_skill"], "SKILLED": 150}
    resp = client.put(f"/api/v1/pay-groups/{groups['CCTV'].id}", json={"params": params, "name": "CCTV site"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["params"]["allowance_per_day_by_skill"]["SKILLED"] == 150
    assert resp.json()["name"] == "CCTV site"


def test_pay_group_update_rejects_bad_params(client, db_session):
    groups = _groups(db_session)
    params = dict(groups["MCBS"].params)
    del params["pf_threshold"]
    params["bonus_rate"] = "x"
    resp = client.put(f"/api/v1/pay-groups/{groups['MCBS'].id}", json={"params": params})
    assert resp.status_code == 400
    assert "missing keys: pf_threshold" in resp.json()["detail"]
    assert "'bonus_rate' must be a non-negative number" in resp.json()["detail"]
    assert client.get("/api/v1/pay-groups/9999").status_code == 404


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
    assert "skill_level 'Expert'" in resp.json()["detail"]
    assert client.get(f"/api/v1/employees/{employee.id}/pay-profile").status_code == 404
    assert _set_profile(client, 9999, pay_group_id=groups["CCTV"].id, daily_wage=1).status_code == 404
