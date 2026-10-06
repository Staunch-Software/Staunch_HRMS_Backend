BASIC_COMPONENT = {
    "name": "Basic Pay",
    "code": "basic",  # lowercase on purpose — API should uppercase it
    "component_type": "Earning",
    "status": True,
    "payslip_order": 1,
    "earning_sub_type": "Fixed Pay",
    "is_taxable": "Yes",
    "calculation_method": "Fixed Amount",
    "affects_pf": True,
    "affects_esi": True,
    "is_prorated": True,
    "is_recurring": True,
}


def test_create_and_list_salary_component(client):
    response = client.post("/api/v1/salary-components", json=BASIC_COMPONENT)
    assert response.status_code == 201
    body = response.json()
    assert body["code"] == "BASIC"  # stored uppercase

    list_response = client.get("/api/v1/salary-components")
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1


def test_duplicate_code_rejected(client):
    client.post("/api/v1/salary-components", json=BASIC_COMPONENT)
    response = client.post("/api/v1/salary-components", json=BASIC_COMPONENT)
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_cannot_delete_basic_component(client):
    create_resp = client.post("/api/v1/salary-components", json=BASIC_COMPONENT)
    component_id = create_resp.json()["id"]

    response = client.delete(f"/api/v1/salary-components/{component_id}")
    assert response.status_code == 400
    assert "Cannot delete the core 'BASIC' component." == response.json()["detail"]


def test_can_delete_non_basic_component(client):
    other = dict(BASIC_COMPONENT, code="HRA", name="House Rent Allowance")
    create_resp = client.post("/api/v1/salary-components", json=other)
    component_id = create_resp.json()["id"]

    response = client.delete(f"/api/v1/salary-components/{component_id}")
    assert response.status_code == 200
    assert response.json() == {"success": True}
