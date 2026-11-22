"""merge sync, payslip, mobile, regularization heads

Revision ID: f279605c0305
Revises: 65654a5bc391, 9cc8bfe27a02, d4e8a1c7b5f2, e3b9f2a6c418
Create Date: 2026-11-20 18:09:14.622217

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f279605c0305'
down_revision: Union[str, None] = ('65654a5bc391', '9cc8bfe27a02', 'd4e8a1c7b5f2', 'e3b9f2a6c418')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
