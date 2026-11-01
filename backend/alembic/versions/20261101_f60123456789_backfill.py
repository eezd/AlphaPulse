"""Normalize historical items and track the batch's downstream targets."""
from alembic import op
import sqlalchemy as sa
revision = 'f60123456789'
down_revision = 'e5f701234567'
branch_labels = depends_on = None


def upgrade():
    op.create_table('news_ai_reprocess_items',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('job_id', sa.BigInteger(), sa.ForeignKey('news_ai_reprocess_jobs.id'), nullable=False, index=True),
        sa.Column('news_id', sa.BigInteger(), sa.ForeignKey('news_items.id'), nullable=False, index=True),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('state', sa.String(16), nullable=False, index=True), sa.Column('error', sa.Text()),
        sa.Column('started_at', sa.DateTime(timezone=True)), sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('job_id', 'news_id'))
    op.create_table('news_ai_reprocess_targets',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('job_id', sa.BigInteger(), sa.ForeignKey('news_ai_reprocess_jobs.id'), nullable=False, index=True),
        sa.Column('kind', sa.String(16), nullable=False), sa.Column('object_id', sa.BigInteger(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False), sa.UniqueConstraint('job_id', 'kind', 'object_id'))
    op.execute("""INSERT INTO news_ai_reprocess_items(job_id,news_id,content_hash,mode,state)
        SELECT j.id,(i->>'id')::bigint,i->>'hash','legacy',i->>'state'
        FROM news_ai_reprocess_jobs j, jsonb_array_elements(j.items) i
        JOIN news_items n ON n.id=(i->>'id')::bigint ON CONFLICT DO NOTHING""")


def downgrade():
    op.drop_table('news_ai_reprocess_targets')
    op.drop_table('news_ai_reprocess_items')
