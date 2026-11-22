"""Approval workflow settings, payroll maker-checker, opening carry forward 0

Adds the approval_settings table (chain per request type, no-manager
fallback, payroll_maker_checker), payroll_runs.calculated_by / submitted_by /
submitted_at, and sets the 2026 opening carry forward to 0: every 2026
CARRY_FORWARD (carry_forward reference) is offset by a CARRY_FORWARD row
with reference carry_forward_reset. Hand-written.

Revision ID: ab08ed3cb50d
Revises: b82530ba4b90
Create Date: 2026-11-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ab08ed3cb50d'
down_revision: Union[str, None] = 'b82530ba4b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RESET_YEAR = 2026
RESET_NOTE = "Opening carry forward set to 0"


def reset_carry_forward(bind, year: int = RESET_YEAR) -> int:
    """Offsets each employee's year-end carry forward into `year` with a
    negative CARRY_FORWARD row (once per employee and leave type)."""
    rows = bind.execute(sa.text(
        "SELECT employee_id, leave_type_id, SUM(days) FROM leave_ledger l "
        "WHERE l.leave_year = :year AND l.entry_type = 'CARRY_FORWARD' AND l.reference_type = 'carry_forward' "
        "AND NOT EXISTS (SELECT 1 FROM leave_ledger r WHERE r.employee_id = l.employee_id "
        "AND r.leave_type_id = l.leave_type_id AND r.leave_year = l.leave_year "
        "AND r.reference_type = 'carry_forward_reset') "
        "GROUP BY employee_id, leave_type_id"
    ), {"year": year}).fetchall()
    written = 0
    for employee_id, leave_type_id, total in rows:
        if not total:
            continue
        bind.execute(sa.text(
            "INSERT INTO leave_ledger (employee_id, leave_type_id, leave_year, entry_type, days, reference_type, "
            "reference_id, note, created_at) VALUES (:employee_id, :leave_type_id, :year, 'CARRY_FORWARD', :days, "
            "'carry_forward_reset', :ref, :note, CURRENT_TIMESTAMP)"
        ), {"employee_id": employee_id, "leave_type_id": leave_type_id, "year": year, "days": -total,
            "ref": str(year), "note": RESET_NOTE})
        written += 1
    return written


def upgrade() -> None:
    op.create_table(
        "approval_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("leave_chain", sa.String(length=20), nullable=False, server_default="MANAGER_HR"),
        sa.Column("permission_chain", sa.String(length=20), nullable=False, server_default="MANAGER_HR"),
        sa.Column("wfh_chain", sa.String(length=20), nullable=False, server_default="MANAGER_HR"),
        sa.Column("regularization_chain", sa.String(length=20), nullable=False, server_default="MANAGER_HR"),
        sa.Column("no_manager_fallback", sa.String(length=20), nullable=False, server_default="HR"),
        sa.Column("payroll_maker_checker", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("payroll_runs", sa.Column("calculated_by", sa.String(length=150), nullable=True))
    op.add_column("payroll_runs", sa.Column("submitted_by", sa.String(length=150), nullable=True))
    op.add_column("payroll_runs", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    reset_carry_forward(op.get_bind())


def downgrade() -> None:
    op.execute("DELETE FROM leave_ledger WHERE reference_type = 'carry_forward_reset'")
    op.drop_column("payroll_runs", "submitted_at")
    op.drop_column("payroll_runs", "submitted_by")
    op.drop_column("payroll_runs", "calculated_by")
    op.drop_table("approval_settings")
