"""Add event timelines, grouping history and explicit reprocessing jobs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'd4e6f8012345'
down_revision = 'c3d5e7f90123'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('news_ai_summaries', sa.Column('review_config', JSONB))
    op.add_column('news_ai_summaries', sa.Column('replacement_ids', JSONB, nullable=False, server_default='[]'))
    op.add_column('news_ai_summary_articles', sa.Column('active', sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column('news_ai_summary_articles', sa.Column('unit', JSONB, nullable=True))
    op.create_table('news_ai_events', sa.Column('id', sa.BigInteger, primary_key=True), sa.Column('title', sa.Text, nullable=False),
        sa.Column('state', sa.String(16), nullable=False), sa.Column('revision', sa.Integer, nullable=False), sa.Column('published_revision', sa.Integer, nullable=False),
        sa.Column('result', JSONB), sa.Column('error', sa.Text), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False), sa.Column('review_config', JSONB))
    op.create_index('ix_news_ai_events_due_at', 'news_ai_events', ['due_at'])
    op.create_table('news_ai_event_members', sa.Column('package_id', sa.BigInteger, sa.ForeignKey('news_ai_summaries.id'), primary_key=True),
        sa.Column('event_id', sa.BigInteger, sa.ForeignKey('news_ai_events.id'), nullable=False), sa.Column('reason', sa.Text, nullable=False))
    op.create_index('ix_news_ai_event_members_event_id', 'news_ai_event_members', ['event_id'])
    op.create_table('news_ai_event_versions', sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('event_id', sa.BigInteger, sa.ForeignKey('news_ai_events.id'), nullable=False), sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('input', JSONB, nullable=False), sa.Column('result', JSONB), sa.Column('rules', JSONB, nullable=False),
        sa.Column('state', sa.String(16), nullable=False), sa.Column('error', sa.Text), sa.Column('usage', JSONB),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_news_ai_event_versions_event_id', 'news_ai_event_versions', ['event_id'])
    op.create_table('news_ai_grouping_versions', sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('analysis_id', sa.BigInteger, sa.ForeignKey('news_ai_article_analyses.id'), nullable=False),
        sa.Column('input', JSONB, nullable=False), sa.Column('result', JSONB), sa.Column('rules', JSONB),
        sa.Column('state', sa.String(16), nullable=False), sa.Column('error', sa.Text),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_news_ai_grouping_versions_analysis_id', 'news_ai_grouping_versions', ['analysis_id'])
    op.execute('INSERT INTO news_ai_grouping_versions (analysis_id,input,result,rules,state,error) SELECT id,input,result,rules,state,error FROM news_ai_article_analyses')
    op.create_table('news_ai_package_corrections', sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('fingerprint', sa.String(64), nullable=False, unique=True), sa.Column('input', JSONB, nullable=False),
        sa.Column('result', JSONB, nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table('news_ai_reprocess_jobs', sa.Column('id', sa.BigInteger, primary_key=True), sa.Column('state', sa.String(16), nullable=False),
        sa.Column('settings', JSONB, nullable=False), sa.Column('items', JSONB, nullable=False), sa.Column('position', sa.Integer, nullable=False),
        sa.Column('failures', JSONB, nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_news_ai_reprocess_jobs_state', 'news_ai_reprocess_jobs', ['state'])
    op.create_table('news_ai_event_links', sa.Column('package_id', sa.BigInteger, sa.ForeignKey('news_ai_summaries.id'), primary_key=True), sa.Column('event_id', sa.BigInteger, sa.ForeignKey('news_ai_events.id'), primary_key=True), sa.Column('reason', sa.Text, nullable=False), sa.Column('package_revision', sa.Integer, nullable=False))
    op.execute('UPDATE risk_settings SET enabled=false')


def downgrade():
    for table in ('news_ai_event_links', 'news_ai_reprocess_jobs', 'news_ai_package_corrections', 'news_ai_grouping_versions', 'news_ai_event_versions', 'news_ai_event_members', 'news_ai_events'):
        op.drop_table(table)
    op.drop_column('news_ai_summary_articles', 'unit')
    op.drop_column('news_ai_summary_articles', 'active')
    op.drop_column('news_ai_summaries', 'replacement_ids')
    op.drop_column('news_ai_summaries', 'review_config')
