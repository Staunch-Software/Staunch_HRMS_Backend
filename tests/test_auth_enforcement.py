"""Regression test for the auth-enforcement gap: every non-auth router now
requires a valid bearer token (see app/api/v1/*.py's `APIRouter(...,
dependencies=[Depends(get_current_user)])`). This asserts, for a
representative sample of routes across employees, salary-components,
pay-schedules and payroll-runs, that the same request is refused with 401
when unauthenticated and succeeds with a valid token — the actual
regression check for closing that gap.

`client` (see conftest.py) is pre-authenticated by default, so "no token" is
exercised by explicitly overriding the Authorization header per-request.
"""
NO_AUTH = {"Authorization": ""}


def test_employees_list_requires_auth(client):
    unauth = client.get("/api/v1/employees", headers=NO_AUTH)
    assert unauth.status_code == 401

    authed = client.get("/api/v1/employees")
    assert authed.status_code == 200


def test_salary_components_list_requires_auth(client):
    unauth = client.get("/api/v1/salary-components", headers=NO_AUTH)
    assert unauth.status_code == 401

    authed = client.get("/api/v1/salary-components")
    assert authed.status_code == 200


def test_pay_schedules_list_requires_auth(client):
    unauth = client.get("/api/v1/pay-schedules", headers=NO_AUTH)
    assert unauth.status_code == 401

    authed = client.get("/api/v1/pay-schedules")
    assert authed.status_code == 200


def test_payroll_runs_list_requires_auth(client):
    unauth = client.get("/api/v1/payroll-runs", headers=NO_AUTH)
    assert unauth.status_code == 401

    authed = client.get("/api/v1/payroll-runs")
    assert authed.status_code == 200


def test_health_stays_public(client):
    """/health is a liveness check mounted outside the versioned API and
    must stay reachable with no token at all."""
    response = client.get("/health", headers=NO_AUTH)
    assert response.status_code == 200


def test_login_stays_public(client, seeded_admin):
    """POST /auth/login must remain reachable with no token — it's how a
    token is obtained in the first place."""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "Test1234!"},
        headers=NO_AUTH,
    )
    assert response.status_code == 200
    assert "access_token" in response.json()
