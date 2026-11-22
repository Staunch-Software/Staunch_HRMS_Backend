"""attendance overrides (HR uploads and manual day corrections)

Revision ID: 55b365b8e53f
Revises: 27044f7c4111
Create Date: 2026-11-15 00:00:00.000000

Hand-written: only the attendance_overrides table. A parallel branch adds
attendance settings / leave tables; the heads are merged separately.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "55b365b8e53f"
down_revision: Union[str, None] = "27044f7c4111"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "attendance_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("employee_code", sa.String(length=30), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("punch_in", sa.DateTime(), nullable=True),
        sa.Column("punch_out", sa.DateTime(), nullable=True),
        sa.Column("status_code", sa.String(length=10), nullable=True),
        sa.Column("leave_type_code", sa.String(length=20), nullable=True),
        sa.Column("half", sa.String(length=12), nullable=False, server_default="FULL"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("import_batch_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=150), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_by", sa.String(length=150), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_attendance_overrides_work_date"), "attendance_overrides", ["work_date"])
    op.create_index(
        "ix_attendance_override_lookup", "attendance_overrides", ["employee_id", "work_date", "is_active"]
    )
    op.create_index(
        "uq_attendance_override_active", "attendance_overrides", ["employee_id", "work_date"],
        unique=True, postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_attendance_override_active", table_name="attendance_overrides")
    op.drop_index("ix_attendance_override_lookup", table_name="attendance_overrides")
    op.drop_index(op.f("ix_attendance_overrides_work_date"), table_name="attendance_overrides")
    op.drop_table("attendance_overrides")
