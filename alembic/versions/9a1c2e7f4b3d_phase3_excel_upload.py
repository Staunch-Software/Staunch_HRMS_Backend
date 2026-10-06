"""phase3 excel upload

Revision ID: 9a1c2e7f4b3d
Revises: 780aa822956e
Create Date: 2026-09-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a1c2e7f4b3d'
down_revision: Union[str, None] = '780aa822956e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ties a variable_pay_inputs row back to the Excel upload batch that
    # created it. Null for manually-entered rows (source="Manual").
    op.add_column(
        'variable_pay_inputs',
        sa.Column('import_batch_id', sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('variable_pay_inputs', 'import_batch_id')
