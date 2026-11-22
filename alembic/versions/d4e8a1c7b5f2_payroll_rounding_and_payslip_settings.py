"""payroll rounding and payslip settings

Hand-written: singleton payroll_rounding_settings and payslip_settings
(company profile, logo bytes, template and field switches).

Revision ID: d4e8a1c7b5f2
Revises: b82530ba4b90
Create Date: 2026-11-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e8a1c7b5f2"
down_revision: Union[str, None] = "b82530ba4b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "payroll_rounding_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("default_net_rounding", sa.String(length=20), nullable=False),
        sa.Column("default_line_rounding", sa.String(length=20), nullable=False),
        sa.Column("default_line_decimals", sa.Integer(), nullable=False),
        sa.Column("tds_rounding", sa.String(length=20), nullable=False),
        sa.Column("attendance_day_step", sa.String(length=10), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["updated_by"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "payslip_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("company_address", sa.Text(), nullable=True),
        sa.Column("company_cin", sa.String(length=30), nullable=True),
        sa.Column("company_gstin", sa.String(length=20), nullable=True),
        sa.Column("logo", sa.LargeBinary(), nullable=True),
        sa.Column("logo_mime", sa.String(length=50), nullable=True),
        sa.Column("template", sa.String(length=20), nullable=False),
        sa.Column("show_pan", sa.Boolean(), nullable=False),
        sa.Column("show_uan", sa.Boolean(), nullable=False),
        sa.Column("show_bank", sa.Boolean(), nullable=False),
        sa.Column("show_attendance", sa.Boolean(), nullable=False),
        sa.Column("show_leave_balance", sa.Boolean(), nullable=False),
        sa.Column("show_ytd", sa.Boolean(), nullable=False),
        sa.Column("show_employer_contrib", sa.Boolean(), nullable=False),
        sa.Column("footer_note", sa.Text(), nullable=True),
        sa.Column("pdf_password", sa.String(length=10), nullable=False),
        sa.Column("release_on", sa.String(length=10), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["updated_by"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("payslip_settings")
    op.drop_table("payroll_rounding_settings")
