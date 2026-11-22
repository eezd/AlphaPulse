"""持久保存单篇长文分段与在途状态，不改写既有分析。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'e52345678901'
down_revision = 'd41234567890'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('briefing_analysis_parts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('analysis_id', sa.BigInteger(), sa.ForeignKey('briefing_analyses.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('stage', sa.String(16), nullable=False),
        sa.Column('part_index', sa.Integer(), nullable=False),
        sa.Column('start_offset', sa.Integer()),
        sa.Column('end_offset', sa.Integer()),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('control_revision', sa.Integer(), nullable=False),
        sa.Column('input', JSONB(), nullable=False),
        sa.Column('rules', JSONB(), nullable=False),
        sa.Column('output', JSONB(none_as_null=True)),
        sa.Column('usage', JSONB(none_as_null=True)),
        sa.Column('reused_from', sa.BigInteger(), sa.ForeignKey('briefing_analysis_parts.id', ondelete='RESTRICT')),
        sa.Column('error', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('analysis_id', 'stage', 'part_index', name='uq_briefing_analysis_part'))
    op.create_index('ix_briefing_analysis_parts_analysis_id', 'briefing_analysis_parts', ['analysis_id'])
    op.create_index('ix_briefing_analysis_part_fingerprint', 'briefing_analysis_parts', ['input_hash', 'state'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM briefing_analysis_parts)')):
        raise RuntimeError('已有长文分析分段审计，拒绝删除')
    op.drop_index('ix_briefing_analysis_part_fingerprint', table_name='briefing_analysis_parts')
    op.drop_index('ix_briefing_analysis_parts_analysis_id', table_name='briefing_analysis_parts')
    op.drop_table('briefing_analysis_parts')
