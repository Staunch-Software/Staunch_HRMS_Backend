"""employee master fields

Adds the full employee master-data columns (personal, statutory, education,
apprenticeship/contract, structured address) used by the Add/Edit form and
the Excel master import, and makes work_locations.state_code nullable so an
imported location whose state can't be inferred is left blank, not guessed.

Revision ID: b2d4f6a8c012
Revises: b7e21c9a4f08
Create Date: 2026-11-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d4f6a8c012"
down_revision: Union[str, None] = "b7e21c9a4f08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMPLOYEE_COLUMNS = [
    ("middle_name", sa.String(length=100)),
    ("date_of_birth", sa.Date()),
    ("marital_status", sa.String(length=30)),
    ("blood_group", sa.String(length=10)),
    ("father_spouse_name", sa.String(length=150)),
    ("relation", sa.String(length=50)),
    ("emergency_contact_name", sa.String(length=150)),
    ("emergency_contact_number", sa.String(length=50)),
    ("permanent_address", sa.Text()),
    ("address_line1", sa.String(length=255)),
    ("address_line2", sa.String(length=255)),
    ("city", sa.String(length=100)),
    ("state", sa.String(length=100)),
    ("country", sa.String(length=100)),
    ("postal_code", sa.String(length=20)),
    ("aadhaar_number", sa.String(length=20)),
    ("pan_number", sa.String(length=20)),
    ("employment_type", sa.String(length=30)),
    ("pf_applicable", sa.Boolean()),
    ("uan_number", sa.String(length=30)),
    ("esi_applicable", sa.Boolean()),
    ("esi_number", sa.String(length=30)),
    ("insurance_applicable", sa.Boolean()),
    ("geographic_location", sa.String(length=100)),
    ("client", sa.String(length=150)),
    ("client_branch", sa.String(length=150)),
    ("remarks", sa.Text()),
    ("highest_qualification", sa.String(length=150)),
    ("certifications", sa.String(length=255)),
    ("year_of_passing", sa.String(length=20)),
    ("university", sa.String(length=200)),
    ("apprenticeship_type", sa.String(length=50)),
    ("enrollment_id", sa.String(length=50)),
    ("contract_no", sa.String(length=50)),
    ("contract_start_date", sa.Date()),
    ("contract_end_date", sa.Date()),
    ("stipend_amount", sa.Numeric(precision=12, scale=2)),
]


def upgrade() -> None:
    for name, column_type in EMPLOYEE_COLUMNS:
        op.add_column("employees", sa.Column(name, column_type, nullable=True))
    op.alter_column("work_locations", "state_code", existing_type=sa.String(length=5), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE work_locations SET state_code = '' WHERE state_code IS NULL")
    op.alter_column("work_locations", "state_code", existing_type=sa.String(length=5), nullable=False)
    for name, _ in reversed(EMPLOYEE_COLUMNS):
        op.drop_column("employees", name)
