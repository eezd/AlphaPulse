"""Persist realtime boundaries and enforce one active call per subject."""
from alembic import op
import sqlalchemy as sa

revision = 'dd4567890123'
down_revision = 'dc3456789012'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('news_research_settings', sa.Column('realtime_from', sa.DateTime(timezone=True)))
    op.add_column('news_research_events', sa.Column('realtime_pending', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('uq_news_attempt_active', 'news_research_attempts', ['package_id'], unique=True,
                    postgresql_where=sa.text("state = 'processing'"))
    op.create_index('uq_news_version_active', 'news_research_versions', ['event_id'], unique=True,
                    postgresql_where=sa.text("state = 'processing'"))


def downgrade():
    op.drop_index('uq_news_version_active', table_name='news_research_versions')
    op.drop_index('uq_news_attempt_active', table_name='news_research_attempts')
    op.drop_column('news_research_events', 'realtime_pending')
    op.drop_column('news_research_settings', 'realtime_from')
