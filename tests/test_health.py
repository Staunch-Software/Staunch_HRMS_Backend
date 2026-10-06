def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_payroll_summary_shape(client):
    response = client.get("/api/v1/payroll-summary")
    assert response.status_code == 200
    body = response.json()
    for key in ["totalPayroll", "averageSalary", "employeesPaid", "taxesWithheld", "deductions"]:
        assert key in body
