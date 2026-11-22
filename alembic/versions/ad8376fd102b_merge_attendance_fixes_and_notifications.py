"""merge attendance fixes and notifications

Revision ID: ad8376fd102b
Revises: 71272a4984c2, 66b43a942cce
Create Date: 2026-11-15 23:56:55.034859

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ad8376fd102b'
down_revision: Union[str, None] = ('71272a4984c2', '66b43a942cce')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
