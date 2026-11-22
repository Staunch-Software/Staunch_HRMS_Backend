from sqlalchemy import text

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


def _raw(db_session, sql):
    return db_session.execute(text(sql)).scalar()


def test_sensitive_employee_fields_masked_encrypted_and_kept_on_masked_put(client, db_session):
    created = client.post(
        "/api/v1/employees", json=dict(EMPLOYEE, aadhaar_number="111122223333", pan_number="ABCDE1234F")
    ).json()
    employee_id = created["id"]
    assert created["aadhaar_number"] == "XXXX XXXX 3333" and created["pan_number"] == "XXXXXX234F"

    listed = client.get("/api/v1/employees").json()
    assert listed[0]["aadhaar_number"] == "XXXX XXXX 3333" and listed[0]["pan_number"] == "XXXXXX234F"

    raw_aadhaar = _raw(db_session, f"SELECT aadhaar_number FROM employees WHERE id = {employee_id}")
    raw_pan = _raw(db_session, f"SELECT pan_number FROM employees WHERE id = {employee_id}")
    assert raw_aadhaar.startswith("enc:v1:") and raw_pan.startswith("enc:v1:")
    assert "111122223333" not in raw_aadhaar

    # The edit form sends back what it was given (masked) or null: both keep the stored value.
    for aadhaar, pan in (("XXXX XXXX 3333", "XXXXXX234F"), (None, None)):
        resp = client.put(
            f"/api/v1/employees/{employee_id}",
            json=dict(EMPLOYEE, first_name="Edited", aadhaar_number=aadhaar, pan_number=pan),
        )
        assert resp.status_code == 200, resp.text
        sensitive = client.get(f"/api/v1/employees/{employee_id}/sensitive").json()
        assert sensitive["aadhaar_number"] == "111122223333" and sensitive["pan_number"] == "ABCDE1234F"

    resp = client.put(
        f"/api/v1/employees/{employee_id}",
        json=dict(EMPLOYEE, aadhaar_number="999988887777", pan_number="AXXPX1234X"),
    )
    assert resp.json()["pan_number"] == "XXXXXX234X"
    sensitive = client.get(f"/api/v1/employees/{employee_id}/sensitive").json()
    assert sensitive["aadhaar_number"] == "999988887777" and sensitive["pan_number"] == "AXXPX1234X"


def test_bank_account_masked_and_kept_on_masked_put(client, db_session):
    employee_id = client.post("/api/v1/employees", json=EMPLOYEE).json()["id"]

    put_resp = client.put(f"/api/v1/employees/{employee_id}/bank-account", json=BANK_ACCOUNT)
    assert put_resp.status_code == 200
    body = client.get(f"/api/v1/employees/{employee_id}/bank-account").json()
    assert body["account_number"] == "XXXXXXXX9012" and body["account_number_last4"] == "9012"
    assert put_resp.json()["account_number"] == "XXXXXXXX9012"

    raw = _raw(db_session, f"SELECT account_number FROM employee_bank_accounts WHERE employee_id = {employee_id}")
    assert raw.startswith("enc:v1:") and "123456789012" not in raw

    # Re-PUT with the masked value (what the edit form re-sends) and with no number at all.
    for account in ("XXXXXXXX9012", None):
        resp = client.put(
            f"/api/v1/employees/{employee_id}/bank-account",
            json=dict(BANK_ACCOUNT, account_number=account, bank_name="SBI Main"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bank_name"] == "SBI Main"
        sensitive = client.get(f"/api/v1/employees/{employee_id}/sensitive").json()
        assert sensitive["account_number"] == "123456789012"

    assert client.put(
        f"/api/v1/employees/{employee_id}/bank-account", json=dict(BANK_ACCOUNT, account_number="12AB34")
    ).status_code == 422

    client.put(f"/api/v1/employees/{employee_id}/bank-account", json=dict(BANK_ACCOUNT, account_number="555566667777"))
    assert client.get(f"/api/v1/employees/{employee_id}/sensitive").json()["account_number"] == "555566667777"


def test_masked_bank_account_without_stored_account_rejected(client):
    employee_id = client.post("/api/v1/employees", json=EMPLOYEE).json()["id"]
    resp = client.put(
        f"/api/v1/employees/{employee_id}/bank-account", json=dict(BANK_ACCOUNT, account_number="XXXXXXXX9012")
    )
    assert resp.status_code == 422
    assert client.get(f"/api/v1/employees/{employee_id}/sensitive").json()["account_number"] is None
