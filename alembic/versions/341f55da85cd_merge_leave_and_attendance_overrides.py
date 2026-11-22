"""merge leave and attendance overrides

Revision ID: 341f55da85cd
Revises: 55b365b8e53f, c90ae2316d3c
Create Date: 2026-11-15 19:23:30.224063

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '341f55da85cd'
down_revision: Union[str, None] = ('55b365b8e53f', 'c90ae2316d3c')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
