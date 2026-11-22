"""TDS 2026-27: regime-aware TDS configs, surcharge bands, declaration fields

Schema: tds_configs.regime + surcharge_bands; employee_tax_declarations
.previous_employer_tds + old_regime_deductions.
Data: the FY 2026-27 rows seeded earlier carried FY 2024-25 figures
(3/6/9/12/15L slabs, 7L/25,000 rebate). Their slabs are deleted, their
config is deactivated (notes prefixed), and the verified New and Old regime
rows are inserted. Hand-written.

Revision ID: cc98a420e700
Revises: 908b0564cacd
Create Date: 2026-11-15 10:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.models.json_type


# revision identifiers, used by Alembic.
revision: str = 'cc98a420e700'
down_revision: Union[str, None] = '908b0564cacd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FY = "2026-27"
SUPERSEDED_PREFIX = "[Superseded by migration cc98a420e700] "
VERIFIED_NOTE = (
    "Verified against the incometax.gov.in tax-rate table for AY 2026-27; Union "
    "Budget 2026-27 left the salary slabs, standard deduction, rebate, surcharge "
    "and cess unchanged for tax year 2026-27. Individual below 60 years."
)
OLD_UNVERIFIED_NOTE = (
    "Seeded from general knowledge, not verified against the current Finance "
    "Act as of this build — confirm against the latest Union Budget / CBDT "
    "notification before running live payroll."
)

CONFIGS = [
    {
        "regime": "New", "standard_deduction": 75000, "cess_rate": 0.04,
        "rebate_87a_income_limit": 1200000, "rebate_87a_max_amount": 60000,
        "surcharge_bands": [
            {"above": 5000000, "rate": 0.10},
            {"above": 10000000, "rate": 0.15},
            {"above": 20000000, "rate": 0.25},
        ],
    },
    {
        "regime": "Old", "standard_deduction": 50000, "cess_rate": 0.04,
        "rebate_87a_income_limit": 500000, "rebate_87a_max_amount": 12500,
        "surcharge_bands": [
            {"above": 5000000, "rate": 0.10},
            {"above": 10000000, "rate": 0.15},
            {"above": 20000000, "rate": 0.25},
            {"above": 50000000, "rate": 0.37},
        ],
    },
]

SLABS = {
    "New": [
        (0, 400000, 0.0),
        (400000, 800000, 0.05),
        (800000, 1200000, 0.10),
        (1200000, 1600000, 0.15),
        (1600000, 2000000, 0.20),
        (2000000, 2400000, 0.25),
        (2400000, None, 0.30),
    ],
    "Old": [
        (0, 250000, 0.0),
        (250000, 500000, 0.05),
        (500000, 1000000, 0.20),
        (1000000, None, 0.30),
    ],
}

# What the previous seed wrote, restored on downgrade.
PREVIOUS_NEW_SLABS = [
    (0, 300000, 0.0),
    (300000, 600000, 0.05),
    (600000, 900000, 0.10),
    (900000, 1200000, 0.15),
    (1200000, 1500000, 0.20),
    (1500000, None, 0.30),
]

tds_configs = sa.table(
    "tds_configs",
    sa.column("id", sa.Integer),
    sa.column("financial_year", sa.String),
    sa.column("regime", sa.String),
    sa.column("standard_deduction", sa.Numeric),
    sa.column("cess_rate", sa.Numeric),
    sa.column("rebate_87a_income_limit", sa.Numeric),
    sa.column("rebate_87a_max_amount", sa.Numeric),
    sa.column("surcharge_bands", app.models.json_type.JSONType),
    sa.column("is_active", sa.Boolean),
    sa.column("is_verified", sa.Boolean),
    sa.column("notes", sa.Text),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

tds_slabs = sa.table(
    "tds_slabs",
    sa.column("id", sa.Integer),
    sa.column("regime", sa.String),
    sa.column("financial_year", sa.String),
    sa.column("income_from", sa.Numeric),
    sa.column("income_to", sa.Numeric),
    sa.column("rate", sa.Numeric),
    sa.column("is_verified", sa.Boolean),
    sa.column("notes", sa.Text),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _slab_rows(regime, slabs, verified, note, now):
    return [
        {
            "regime": regime, "financial_year": FY, "income_from": lo, "income_to": hi, "rate": rate,
            "is_verified": verified, "notes": note, "created_at": now, "updated_at": now,
        }
        for lo, hi, rate in slabs
    ]


def upgrade() -> None:
    op.add_column('tds_configs', sa.Column('regime', sa.String(length=10), server_default='New', nullable=False))
    op.add_column(
        'tds_configs',
        sa.Column('surcharge_bands', app.models.json_type.JSONType(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column('employee_tax_declarations', sa.Column('previous_employer_tds', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('employee_tax_declarations', sa.Column('old_regime_deductions', sa.Numeric(precision=12, scale=2), nullable=True))

    now = datetime.now(timezone.utc)
    op.execute(tds_slabs.delete().where(tds_slabs.c.financial_year == FY))
    op.execute(
        tds_configs.update()
        .where(tds_configs.c.financial_year == FY)
        .values(
            is_active=False,
            notes=sa.literal(SUPERSEDED_PREFIX) + sa.func.coalesce(tds_configs.c.notes, ""),
            updated_at=now,
        )
    )
    op.bulk_insert(tds_configs, [
        {
            "financial_year": FY, **config, "is_active": True, "is_verified": True, "notes": VERIFIED_NOTE,
            "created_at": now, "updated_at": now,
        }
        for config in CONFIGS
    ])
    op.bulk_insert(tds_slabs, [
        row for regime, slabs in SLABS.items() for row in _slab_rows(regime, slabs, True, VERIFIED_NOTE, now)
    ])


def downgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    op.execute(tds_slabs.delete().where(tds_slabs.c.financial_year == FY))
    op.execute(tds_configs.delete().where(tds_configs.c.financial_year == FY, tds_configs.c.notes == VERIFIED_NOTE))
    superseded = tds_configs.c.notes.startswith(SUPERSEDED_PREFIX)
    restored = bind.execute(
        sa.select(sa.func.count()).select_from(tds_configs).where(tds_configs.c.financial_year == FY, superseded)
    ).scalar()
    op.execute(
        tds_configs.update()
        .where(tds_configs.c.financial_year == FY, superseded)
        .values(
            is_active=True,
            notes=sa.func.substr(tds_configs.c.notes, len(SUPERSEDED_PREFIX) + 1),
            updated_at=now,
        )
    )
    if restored:
        op.bulk_insert(tds_slabs, _slab_rows("New", PREVIOUS_NEW_SLABS, False, OLD_UNVERIFIED_NOTE, now))

    op.drop_column('employee_tax_declarations', 'old_regime_deductions')
    op.drop_column('employee_tax_declarations', 'previous_employer_tds')
    op.drop_column('tds_configs', 'surcharge_bands')
    op.drop_column('tds_configs', 'regime')
