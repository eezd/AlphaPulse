"""news signals

Revision ID: c5e8a1b3d207
Revises: a7d3e5f20c18
Create Date: 2026-10-16 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e8a1b3d207'
down_revision: Union[str, Sequence[str], None] = 'a7d3e5f20c18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'news_signals',
        sa.Column('news_id', sa.BigInteger(), nullable=False),
        sa.Column('category', sa.String(length=16), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('labeled_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('prices', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['news_id'], ['news_items.id']),
        sa.PrimaryKeyConstraint('news_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('news_signals')
