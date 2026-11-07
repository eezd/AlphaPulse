"""Persist personal news bookmarks without modifying source news."""
from alembic import op
import sqlalchemy as sa

revision = 'de5678901234'
down_revision = 'dd4567890123'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('news_bookmarks',
        sa.Column('username', sa.String(128), primary_key=True),
        sa.Column('news_id', sa.BigInteger(), sa.ForeignKey('news_items.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    op.drop_table('news_bookmarks')
