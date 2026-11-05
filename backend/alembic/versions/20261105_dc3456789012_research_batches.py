"""Persist frozen two-stage research batches without rewriting research history."""
from alembic import op
import sqlalchemy as sa

revision = 'dc3456789012'
down_revision = 'db2345678901'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('news_research_batches',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('phase', sa.String(20), nullable=False),
        sa.Column('cutoff_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_news_research_batches_phase', 'news_research_batches', ['phase'])
    op.create_table('news_research_batch_packages',
        sa.Column('batch_id', sa.BigInteger(), sa.ForeignKey('news_research_batches.id'), primary_key=True),
        sa.Column('package_id', sa.BigInteger(), sa.ForeignKey('news_research_packages.id'), primary_key=True),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('state', sa.String(20), nullable=False))
    op.create_table('news_research_batch_events',
        sa.Column('batch_id', sa.BigInteger(), sa.ForeignKey('news_research_batches.id'), primary_key=True),
        sa.Column('event_id', sa.BigInteger(), sa.ForeignKey('news_research_events.id'), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False))


def downgrade():
    op.drop_table('news_research_batch_events')
    op.drop_table('news_research_batch_packages')
    op.drop_table('news_research_batches')
