import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.core.security import hash_password
from app.main import app
from app.models import Base
from app.models.user import AdminUser

# In-memory SQLite, fresh per test session — fine for Phase 1's smoke tests
# since there is no JSONB-heavy logic yet (see backend/README.md).
TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db_session():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Test1234!"


@pytest.fixture()
def seeded_admin(db_session):
    admin = AdminUser(username=ADMIN_USERNAME, hashed_password=hash_password(ADMIN_PASSWORD), is_active=True)
    db_session.add(admin)
    db_session.commit()
    return admin


@pytest.fixture()
def client(db_session, seeded_admin):
    """A TestClient authenticated as the seeded admin user by default.

    Every router except POST /auth/login now requires a bearer token, so
    this fixture logs in once and attaches the token as a default header —
    every request the returned client makes carries it automatically,
    without every existing test needing to pass it explicitly.

    A test that specifically needs to exercise UNauthenticated behavior
    overrides the header for that one call, e.g.
    `client.get(url, headers={"Authorization": ""})`.
    """
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        login_resp = test_client.post(
            "/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        assert login_resp.status_code == 200, login_resp.text
        token = login_resp.json()["access_token"]
        test_client.headers.update({"Authorization": f"Bearer {token}"})
        yield test_client
    app.dependency_overrides.clear()
