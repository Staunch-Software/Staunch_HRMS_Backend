"""merge encryption tds roles

Revision ID: 27044f7c4111
Revises: 2237601a29fe, 68c2af95dd6b, cc98a420e700
Create Date: 2026-11-15 14:02:50.373687

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '27044f7c4111'
down_revision: Union[str, None] = ('2237601a29fe', '68c2af95dd6b', 'cc98a420e700')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
