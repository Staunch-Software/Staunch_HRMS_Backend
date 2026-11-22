"""merge wfh and monthly cl credit

Revision ID: b82530ba4b90
Revises: 8e7ff8df9594, 8675f75c1fcf
Create Date: 2026-11-17 17:41:10.626969

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b82530ba4b90'
down_revision: Union[str, None] = ('8e7ff8df9594', '8675f75c1fcf')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
