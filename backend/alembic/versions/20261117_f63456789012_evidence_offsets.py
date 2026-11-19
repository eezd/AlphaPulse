"""保留长文分段引文区间的唯一逐字校正审计。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'f63456789012'
down_revision = 'e52345678901'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('briefing_analysis_parts', sa.Column('evidence_corrections', JSONB(none_as_null=True)))


def downgrade():
    if op.get_bind().scalar(sa.text(
            'SELECT EXISTS (SELECT 1 FROM briefing_analysis_parts WHERE evidence_corrections IS NOT NULL)')):
        raise RuntimeError('已有分段引文区间校正审计，拒绝删除')
    op.drop_column('briefing_analysis_parts', 'evidence_corrections')
