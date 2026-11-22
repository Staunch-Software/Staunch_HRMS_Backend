"""Attendance late-upload hold

Adds attendance_policies.late_upload_hold_days: a day with an IN punch but
no OUT younger than this many days is held (no missing-punch rule, no LOP)
while offline devices upload. Hand-written.

Revision ID: 9cc8bfe27a02
Revises: b82530ba4b90
Create Date: 2026-11-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9cc8bfe27a02'
down_revision: Union[str, None] = 'b82530ba4b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("attendance_policies",
                  sa.Column("late_upload_hold_days", sa.Integer(), nullable=False, server_default="2"))


def downgrade() -> None:
    op.drop_column("attendance_policies", "late_upload_hold_days")
