"""pay group formula definitions

Adds pay_groups.definition / definition_version, the
pay_group_definition_versions history table and
payroll_runs.pay_group_definition_version, then converts every pay group
still on a fixed strategy (MONTHLY_SALARIED, DAILY_FLAT_RATE, ...) to
calc_model FORMULA in place: its definition is built from its CURRENT params
by the app's converter (so HR-edited rates carry over), the old params stay
on the row for reference, and version 1 is recorded in the history.
Hand-written.

Revision ID: 4cecedcccfb1
Revises: ad8376fd102b
Create Date: 2026-11-16 12:00:00.000000

"""
import re
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.models.json_type


# revision identifiers, used by Alembic.
revision: str = '4cecedcccfb1'
down_revision: Union[str, None] = 'ad8376fd102b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = app.models.json_type.JSONType
NOTE_RE = re.compile(r"from (?:the )?([A-Z_]+) (?:model|params)")

pay_groups = sa.table(
    "pay_groups",
    sa.column("id", sa.Integer), sa.column("code", sa.String), sa.column("calc_model", sa.String),
    sa.column("params", JSON()), sa.column("definition", JSON()), sa.column("definition_version", sa.Integer),
)
versions = sa.table(
    "pay_group_definition_versions",
    sa.column("pay_group_id", sa.Integer), sa.column("version", sa.Integer), sa.column("definition", JSON()),
    sa.column("changed_by", sa.String), sa.column("changed_at", sa.DateTime(timezone=True)),
    sa.column("note", sa.Text),
)


def upgrade() -> None:
    op.add_column('pay_groups', sa.Column('definition', JSON(), nullable=True))
    op.add_column('pay_groups', sa.Column('definition_version', sa.Integer(), nullable=False, server_default='0'))
    op.create_table(
        'pay_group_definition_versions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('definition', JSON(), nullable=False),
        sa.Column('changed_by', sa.String(length=150), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['pay_group_id'], ['pay_groups.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pay_group_id', 'version', name='uq_pay_group_definition_version'),
    )
    op.create_index(op.f('ix_pay_group_definition_versions_pay_group_id'), 'pay_group_definition_versions',
                    ['pay_group_id'], unique=False)
    op.add_column('payroll_runs', sa.Column('pay_group_definition_version', sa.Integer(), nullable=True))
    _convert_groups()


def _convert_groups() -> None:
    from app.services.payroll_engine.formula.converters_wage import CONVERTERS
    from app.services.payroll_engine.formula.history import converted_definition

    conn = op.get_bind()
    now = datetime.now(timezone.utc)
    rows = conn.execute(sa.select(pay_groups.c.id, pay_groups.c.code, pay_groups.c.calc_model,
                                  pay_groups.c.params)).all()
    for row in rows:
        if row.calc_model not in CONVERTERS:
            continue
        definition = {**converted_definition(row.calc_model, row.params or {}, row.code), "version": 1}
        conn.execute(pay_groups.update().where(pay_groups.c.id == row.id).values(
            calc_model="FORMULA", definition=definition, definition_version=1))
        conn.execute(versions.insert().values(
            pay_group_id=row.id, version=1, definition=definition, changed_by="migration", changed_at=now,
            note=f"Converted from {row.calc_model} params (migration 4cecedcccfb1)"))


def downgrade() -> None:
    conn = op.get_bind()
    groups = conn.execute(sa.select(pay_groups.c.id, pay_groups.c.code).where(
        pay_groups.c.calc_model == "FORMULA")).all()
    for group in groups:
        note = conn.execute(sa.select(versions.c.note).where(
            versions.c.pay_group_id == group.id, versions.c.version == 1)).scalar()
        match = NOTE_RE.search(note or "")
        if match is None:
            raise RuntimeError(f"Cannot downgrade: pay group {group.code} exists only as formulas")
        conn.execute(pay_groups.update().where(pay_groups.c.id == group.id).values(calc_model=match.group(1)))
    op.drop_column('payroll_runs', 'pay_group_definition_version')
    op.drop_index(op.f('ix_pay_group_definition_versions_pay_group_id'), table_name='pay_group_definition_versions')
    op.drop_table('pay_group_definition_versions')
    op.drop_column('pay_groups', 'definition_version')
    op.drop_column('pay_groups', 'definition')
