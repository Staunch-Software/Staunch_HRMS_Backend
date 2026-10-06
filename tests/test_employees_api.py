EMPLOYEE = {
    "employee_code": "E1001",
    "first_name": "Vijay",
    "work_email": "vj@staunchtec.com",
    "date_of_joining": "2025-04-15",
    "basic_salary": 60000,
    "mobile_number": "123-456-7890",
    "gender": "Male",
}

BANK_ACCOUNT = {
    "bank_name": "SBI",
    "account_holder_name": "Vijay Kumar",
    "account_number": "123456789012",
    "ifsc_code": "SBIN0001234",
}


def test_create_employee_and_status_patch(client):
    create_resp = client.post("/api/v1/employees", json=EMPLOYEE)
    assert create_resp.status_code == 201
    employee_id = create_resp.json()["id"]

    patch_resp = client.patch(f"/api/v1/employees/{employee_id}/status", json={"status": "Inactive"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "Inactive"


def test_duplicate_employee_code_rejected(client):
    client.post("/api/v1/employees", json=EMPLOYEE)
    response = client.post("/api/v1/employees", json=EMPLOYEE)
    assert response.status_code == 400


def test_bank_account_upsert_and_ifsc_validation(client):
    create_resp = client.post("/api/v1/employees", json=EMPLOYEE)
    employee_id = create_resp.json()["id"]

    put_resp = client.put(f"/api/v1/employees/{employee_id}/bank-account", json=BANK_ACCOUNT)
    assert put_resp.status_code == 200
    assert put_resp.json()["ifsc_code"] == "SBIN0001234"

    get_resp = client.get(f"/api/v1/employees/{employee_id}/bank-account")
    assert get_resp.status_code == 200
    assert get_resp.json()["bank_name"] == "SBI"

    bad_ifsc = dict(BANK_ACCOUNT, ifsc_code="BADCODE")
    bad_resp = client.put(f"/api/v1/employees/{employee_id}/bank-account", json=bad_ifsc)
    assert bad_resp.status_code == 422
