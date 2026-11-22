"""leave types: monthly credit by length of service

Adds leave_types.service_tiers (JSON) and leave_types.joining_cutoff_day
(default 15), then switches Casual Leave (CL) to credit_mode MONTHLY_TIERED
with the company rule: under 1 year of service 1 day a month that lapses at
month end, 1 year or more 2 days a month carried forward. Other leave types
are left unchanged. Hand-written.

Revision ID: 8675f75c1fcf
Revises: 4cecedcccfb1
Create Date: 2026-11-17 12:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8675f75c1fcf"
down_revision: Union[str, None] = "4cecedcccfb1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CL_TIERS = [
    {"min_service_months": 0, "days_per_month": 1, "carry_forward": False, "carry_forward_limit": None},
    {"min_service_months": 12, "days_per_month": 2, "carry_forward": True, "carry_forward_limit": None},
]


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _leave_types():
    return sa.table(
        "leave_types",
        sa.column("code", sa.String()),
        sa.column("credit_mode", sa.String()),
        sa.column("service_tiers", _json()),
    )


def upgrade() -> None:
    op.add_column("leave_types", sa.Column("service_tiers", _json(), nullable=True))
    op.add_column(
        "leave_types",
        sa.Column("joining_cutoff_day", sa.Integer(), nullable=False, server_default="15"),
    )
    table = _leave_types()
    op.execute(
        table.update()
        .where(table.c.code == "CL")
        .values(
            credit_mode="MONTHLY_TIERED",
            service_tiers=sa.cast(sa.literal(json.dumps(CL_TIERS)), _json()),
        )
    )


def downgrade() -> None:
    table = _leave_types()
    op.execute(
        table.update()
        .where(table.c.credit_mode == "MONTHLY_TIERED")
        .values(credit_mode="YEARLY_UPFRONT")
    )
    op.drop_column("leave_types", "joining_cutoff_day")
    op.drop_column("leave_types", "service_tiers")
