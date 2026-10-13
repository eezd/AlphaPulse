"""source history backfill

Revision ID: d41b7c2e9a10
Revises: bf0ffbfcaf9a
Create Date: 2026-10-13 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd41b7c2e9a10'
down_revision: Union[str, Sequence[str], None] = 'bf0ffbfcaf9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sources', sa.Column('history_target', sa.DateTime(timezone=True), nullable=True))
    op.add_column('sources', sa.Column('history_position', sa.DateTime(timezone=True), nullable=True))
    op.add_column('sources', sa.Column('history_done', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sources', 'history_done')
    op.drop_column('sources', 'history_position')
    op.drop_column('sources', 'history_target')
