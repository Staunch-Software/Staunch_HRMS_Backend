"""merge attendance and employee master branches

Revision ID: 4d16f238ecff
Revises: a1c3d5e7f901, b2d4f6a8c012
Create Date: 2026-11-02 19:09:39.415201

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4d16f238ecff'
down_revision: Union[str, None] = ('a1c3d5e7f901', 'b2d4f6a8c012')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
