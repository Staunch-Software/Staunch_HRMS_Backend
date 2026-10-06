"""Fast local dev/demo bootstrap: create_all() + seed. Run with:
    python -m app.db.init_db
For anything with real accumulating data, use Alembic migrations instead
(see backend/alembic and backend/README.md) — this script is a dev
convenience, not the source of schema truth once data exists.
"""
from app.core.db import engine
from app.db.seed.seed_all import seed_all
from app.models import Base

# Ensure every model module is imported (and thus registered on
# Base.metadata) before create_all() runs.
import app.models  # noqa: F401,E402


def init_db():
    Base.metadata.create_all(bind=engine)
    seed_all()


if __name__ == "__main__":
    init_db()
