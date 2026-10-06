"""A JSON column type that uses native JSONB on PostgreSQL and falls back to
SQLAlchemy's generic JSON (TEXT-backed) on SQLite, so the same model works
against both — SQLite is used for fast local dev/tests, Postgres for
anything real (see backend/README.md).
"""
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class JSONType(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())
