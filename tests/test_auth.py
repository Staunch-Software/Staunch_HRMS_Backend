def test_login_success(client, seeded_admin):
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "Test1234!"})
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_login_wrong_password(client, seeded_admin):
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"


def test_me_requires_token(client, seeded_admin):
    # `client` carries a valid token by default (see conftest.py) since
    # almost every route now requires one — override it here to exercise
    # the genuinely unauthenticated path.
    response = client.get("/api/v1/auth/me", headers={"Authorization": ""})
    assert response.status_code == 401


def test_me_with_valid_token(client, seeded_admin):
    login_resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "Test1234!"})
    token = login_resp.json()["access_token"]
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["username"] == "admin"
