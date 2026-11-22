"""Roles (HR / EMPLOYEE), employee logins, the self-service /me router and
reporting managers."""
from app.models.employee import Employee
from app.models.user import AdminUser
from tests.test_employee_import import MASTER_HEADERS, build_workbook, master_row, upload

API = "/api/v1"


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def add_employee(db_session, code, first, last=None, status="Active", manager=None):
    employee = Employee(employee_code=code, first_name=first, last_name=last, status=status,
                        reporting_manager_id=manager.id if manager else None)
    db_session.add(employee)
    db_session.commit()
    return employee


def login(client, username, password):
    return client.post(f"{API}/auth/login", json={"username": username, "password": password})


def create_logins(client, employee_ids):
    response = client.post(f"{API}/employee-logins/bulk-create", json={"employee_ids": employee_ids})
    assert response.status_code == 200, response.text
    return response.json()


def employee_token(client, db_session, code="STEMP0001", first="Asha", last="Kumar", new_password="NewPass123"):
    """Creates an employee + login, changes the temp password, returns (employee, token)."""
    employee = add_employee(db_session, code, first, last)
    temp = create_logins(client, [employee.id])["created"][0]["temporary_password"]
    token = login(client, code, temp).json()["access_token"]
    response = client.post(f"{API}/auth/change-password", headers=bearer(token),
                           json={"current_password": temp, "new_password": new_password})
    assert response.status_code == 200, response.text
    return employee, token


def test_existing_users_default_to_hr(client, db_session):
    body = login(client, "admin", "Test1234!").json()
    assert body["role"] == "HR" and body["must_change_password"] is False
    me = client.get(f"{API}/auth/me").json()
    assert me["role"] == "HR" and me["employee_id"] is None and me["has_reports"] is False
    assert db_session.query(AdminUser).filter_by(username="admin").one().role == "HR"


def test_hr_gets_200_on_hr_routes_but_not_me(client):
    assert client.get(f"{API}/employees").status_code == 200
    assert client.get(f"{API}/employee-logins").status_code == 200
    assert client.get(f"{API}/payroll-runs").status_code == 200
    # An HR login with no employee record has no self-service profile.
    assert client.get(f"{API}/me/profile").status_code == 403


def test_bulk_create_returns_temp_passwords_once_and_they_work(client, db_session):
    a = add_employee(db_session, "STEMP0001", "Asha", "Kumar")
    b = add_employee(db_session, "STEMP0002", "Ravi")
    add_employee(db_session, "STEMP0003", "Gone", status="Inactive")

    result = client.post(f"{API}/employee-logins/bulk-create", json={"all_active": True}).json()
    assert [c["username"] for c in result["created"]] == ["STEMP0001", "STEMP0002"]
    assert result["skipped"] == []
    passwords = {c["employee_id"]: c["temporary_password"] for c in result["created"]}
    assert all(len(p) == 10 for p in passwords.values())

    users = {u.employee_id: u for u in db_session.query(AdminUser).filter(AdminUser.employee_id.isnot(None))}
    assert users[a.id].role == "EMPLOYEE" and users[a.id].must_change_password is True
    assert users[a.id].hashed_password != passwords[a.id]

    # Never retrievable again: the list has no passwords, a re-run skips them.
    rows = {r["employee_code"]: r for r in client.get(f"{API}/employee-logins").json()}
    assert rows["STEMP0001"]["has_login"] is True and "temporary_password" not in rows["STEMP0001"]
    assert "STEMP0003" not in rows
    again = client.post(f"{API}/employee-logins/bulk-create", json={"employee_ids": [a.id, b.id, 999]}).json()
    assert again["created"] == []
    assert sorted(s["reason"] for s in again["skipped"]) == ["Already has a login", "Already has a login",
                                                            "Employee not found"]

    response = login(client, "STEMP0001", passwords[a.id])
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "EMPLOYEE" and response.json()["must_change_password"] is True
    # Employee codes are matched in any spelling/case.
    assert login(client, "stemp-2", passwords[b.id]).status_code == 200
    assert login(client, "STEMP0001", "wrong-password").status_code == 401


def test_must_change_password_flow(client, db_session):
    employee = add_employee(db_session, "STEMP0001", "Asha", "Kumar")
    temp = create_logins(client, [employee.id])["created"][0]["temporary_password"]
    token = login(client, "STEMP0001", temp).json()["access_token"]

    assert client.get(f"{API}/me/profile", headers=bearer(token)).status_code == 403
    me = client.get(f"{API}/auth/me", headers=bearer(token)).json()
    assert me["must_change_password"] is True and me["employee_code"] == "STEMP0001"
    assert me["employee_name"] == "Asha Kumar"

    change = lambda current, new: client.post(  # noqa: E731
        f"{API}/auth/change-password", headers=bearer(token),
        json={"current_password": current, "new_password": new},
    )
    assert change("not-it", "NewPass123").status_code == 400
    assert change(temp, "short").status_code == 400
    assert change(temp, temp).status_code == 400
    assert change(temp, "NewPass123").status_code == 200

    assert client.get(f"{API}/me/profile", headers=bearer(token)).status_code == 200
    assert login(client, "STEMP0001", temp).status_code == 401
    body = login(client, "STEMP0001", "NewPass123").json()
    assert body["must_change_password"] is False


def test_employee_gets_403_on_hr_routes_and_200_on_own_profile(client, db_session):
    manager = add_employee(db_session, "STEMP0009", "Meena", "Iyer")
    employee, token = employee_token(client, db_session)
    employee.reporting_manager_id = manager.id
    employee.aadhaar_number = "111122223333"
    employee.basic_salary = 50000
    db_session.commit()

    for path in ("/employees", "/employee-logins", "/payroll-runs", "/attendance/sync-runs",
                 f"/employees/{employee.id}"):
        assert client.get(f"{API}{path}", headers=bearer(token)).status_code == 403, path
    assert client.post(f"{API}/employee-logins/bulk-create", headers=bearer(token),
                       json={"all_active": True}).status_code == 403

    response = client.get(f"{API}/me/profile", headers=bearer(token))
    assert response.status_code == 200
    profile = response.json()
    assert profile["employee_code"] == "STEMP0001" and profile["name"] == "Asha Kumar"
    assert profile["reporting_manager_name"] == "Meena Iyer"
    for hidden in ("basic_salary", "aadhaar_number", "pan_number", "account_number"):
        assert hidden not in profile

    me = client.get(f"{API}/auth/me", headers=bearer(token)).json()
    assert me["role"] == "EMPLOYEE" and me["has_reports"] is False


def test_has_reports_for_a_manager(client, db_session):
    manager, token = employee_token(client, db_session, code="STEMP0009", first="Meena")
    add_employee(db_session, "STEMP0001", "Asha", manager=manager)
    assert client.get(f"{API}/auth/me", headers=bearer(token)).json()["has_reports"] is True


def test_inactive_employee_cannot_log_in(client, db_session):
    employee, token = employee_token(client, db_session)
    employee.status = "Inactive"
    db_session.commit()

    response = login(client, "STEMP0001", "NewPass123")
    assert response.status_code == 403
    assert "inactive" in response.json()["detail"].lower()
    assert client.get(f"{API}/auth/me", headers=bearer(token)).status_code == 401


def test_disable_enable_and_reset_password(client, db_session):
    employee, token = employee_token(client, db_session)

    row = client.post(f"{API}/employee-logins/{employee.id}/disable").json()
    assert row["login_enabled"] is False
    assert login(client, "STEMP0001", "NewPass123").status_code == 401
    assert client.get(f"{API}/me/profile", headers=bearer(token)).status_code == 401

    assert client.post(f"{API}/employee-logins/{employee.id}/enable").json()["login_enabled"] is True
    assert login(client, "STEMP0001", "NewPass123").status_code == 200

    reset = client.post(f"{API}/employee-logins/{employee.id}/reset-password").json()
    assert reset["username"] == "STEMP0001" and len(reset["temporary_password"]) == 10
    assert login(client, "STEMP0001", "NewPass123").status_code == 401
    body = login(client, "STEMP0001", reset["temporary_password"]).json()
    assert body["must_change_password"] is True

    other = add_employee(db_session, "STEMP0002", "Ravi")
    assert client.post(f"{API}/employee-logins/{other.id}/reset-password").status_code == 404


def test_reporting_manager_self_and_cycle_validation(client, db_session):
    def create(code, first, manager_id=None):
        response = client.post(f"{API}/employees", json={
            "employee_code": code, "first_name": first, "reporting_manager_id": manager_id,
        })
        assert response.status_code == 201, response.text
        return response.json()

    a = create("STEMP0001", "Asha")
    b = create("STEMP0002", "Ravi", a["id"])
    c = create("STEMP0003", "Kiran", b["id"])
    assert b["reporting_manager_name"] == "Asha"
    assert client.post(f"{API}/employees", json={
        "employee_code": "STEMP0004", "first_name": "X", "reporting_manager_id": 999,
    }).status_code == 400

    def set_manager(employee, manager_id):
        return client.put(f"{API}/employees/{employee['id']}", json={
            "employee_code": employee["employee_code"], "first_name": employee["first_name"],
            "reporting_manager_id": manager_id,
        })

    assert set_manager(a, a["id"]).status_code == 400
    assert set_manager(a, b["id"]).status_code == 400  # A -> B -> A
    assert set_manager(a, c["id"]).status_code == 400  # A -> C -> B -> A
    response = set_manager(c, a["id"])
    assert response.status_code == 200 and response.json()["reporting_manager_name"] == "Asha"

    reports = client.get(f"{API}/employees/{a['id']}/direct-reports").json()
    assert sorted(r["employee_code"] for r in reports) == ["STEMP0002", "STEMP0003"]
    assert "aadhaar_number" not in reports[0]

    # An update that doesn't send the field leaves the manager alone.
    response = client.put(f"{API}/employees/{c['id']}", json={"employee_code": "STEMP0003", "first_name": "Kiran"})
    assert response.json()["reporting_manager_id"] == a["id"]


def test_notice_start_date_round_trips(client):
    response = client.post(f"{API}/employees", json={
        "employee_code": "STEMP0001", "first_name": "Asha", "notice_start_date": "2026-11-01",
    })
    assert response.status_code == 201 and response.json()["notice_start_date"] == "2026-11-01"


def test_master_import_maps_reporting_manager_by_code_then_name(client, db_session):
    existing = add_employee(db_session, "STEMP0050", "Old", "Boss")
    headers = MASTER_HEADERS + ["Reporting Manager"]
    rows = [
        master_row("STEMP0001", "Meena Iyer") + [None],
        master_row("STEMP0002", "Asha Kumar") + ["ST-EMP-1"],      # by code
        master_row("STEMP0003", "Ravi Shankar") + ["meena  iyer"],  # by name
        master_row("STEMP0004", "Kiran Rao") + ["Old Boss"],        # existing DB employee by name
        master_row("STEMP0005", "Nobody Here") + ["Unknown Person"],
        master_row("STEMP0006", "Self Ref") + ["STEMP0006"],
    ]
    content = build_workbook({"Employee Master": (headers, rows)})

    preview = upload(client, content).json()
    by_code = {r["employee_code"]: r for r in preview["rows"] if r["employee_code"]}
    assert any("not found" in i["message"] and i["level"] == "warning" for i in by_code["STEMP0005"]["issues"])
    assert any("themself" in i["message"] for i in by_code["STEMP0006"]["issues"])
    assert not any("Reporting Manager" in i["message"] for i in by_code["STEMP0002"]["issues"])

    response = client.post(f"{API}/employees/import/master/commit", json={"batch_id": preview["batch_id"]})
    assert response.status_code == 200, response.text
    db_session.expire_all()
    employees = {e.employee_code: e for e in db_session.query(Employee).all()}
    meena = employees["STEMP0001"]
    assert employees["STEMP0002"].reporting_manager_id == meena.id
    assert employees["STEMP0003"].reporting_manager_id == meena.id
    assert employees["STEMP0004"].reporting_manager_id == existing.id
    assert employees["STEMP0005"].reporting_manager_id is None
    assert employees["STEMP0006"].reporting_manager_id is None

    again = upload(client, content).json()
    assert again["summary"]["updated"] == 0

    # A file that would close a loop (Meena -> Asha while Asha -> Meena) is refused.
    loop = build_workbook({"Employee Master": (headers, [master_row("STEMP0001", "Meena Iyer") + ["STEMP0002"]])})
    row = upload(client, loop).json()["rows"][0]
    assert any("circular" in i["message"] for i in row["issues"])
