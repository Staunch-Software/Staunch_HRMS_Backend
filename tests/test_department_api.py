"""Department manager on the org API, and the new attendance policy settings."""
from tests.leave_helpers import API, make_employee
from tests.test_attendance_settings_api import policy_payload


def test_department_manager_create_update_and_validation(client, db_session):
    meena = make_employee(db_session, "M1", "Meena")
    gone = make_employee(db_session, "M2", "Old", status="Inactive")

    response = client.post(f"{API}/departments", json={"name": "Ops", "code": "ops", "manager_employee_id": meena.id})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["manager_employee_id"] == meena.id and body["manager_name"] == "Meena"
    assert body["manager_code"] == "M1" and body["manager_status"] == "Active"
    dept_id = body["id"]

    bad = client.post(f"{API}/departments", json={"name": "X", "code": "X", "manager_employee_id": gone.id})
    assert bad.status_code == 400 and "active employee" in bad.json()["detail"]
    missing = client.post(f"{API}/departments", json={"name": "Y", "code": "Y", "manager_employee_id": 999})
    assert missing.status_code == 400

    # An update that leaves the manager out keeps it.
    response = client.put(f"{API}/departments/{dept_id}", json={"name": "Operations", "code": "OPS"})
    assert response.status_code == 200 and response.json()["manager_employee_id"] == meena.id
    listed = client.get(f"{API}/departments").json()
    assert listed[0]["manager_name"] == "Meena" and listed[0]["name"] == "Operations"

    # The current manager may stay even after becoming inactive; a new inactive one is refused.
    meena.status = "Inactive"
    db_session.commit()
    same = client.put(f"{API}/departments/{dept_id}",
                      json={"name": "Operations", "code": "OPS", "manager_employee_id": meena.id})
    assert same.status_code == 200
    swap = client.put(f"{API}/departments/{dept_id}",
                      json={"name": "Operations", "code": "OPS", "manager_employee_id": gone.id})
    assert swap.status_code == 400

    cleared = client.put(f"{API}/departments/{dept_id}",
                         json={"name": "Operations", "code": "OPS", "manager_employee_id": None})
    assert cleared.status_code == 200 and cleared.json()["manager_employee_id"] is None
    assert cleared.json()["manager_name"] is None


def test_policy_late_block_size_and_wfh_notice(client):
    default = client.get(f"{API}/attendance-settings/policy").json()["default"]
    assert default["late_block_size"] == 3 and default["wfh_min_notice_days"] == 1
    response = client.put(f"{API}/attendance-settings/policy",
                          json=policy_payload(late_block_size=4, wfh_min_notice_days=2))
    assert response.status_code == 200, response.text
    assert response.json()["late_block_size"] == 4 and response.json()["wfh_min_notice_days"] == 2
    bad = client.put(f"{API}/attendance-settings/policy", json=policy_payload(late_block_size=0))
    assert bad.status_code == 422
