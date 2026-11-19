"""记录机构分析首次接入时间，不自动启用分析或改写历史。"""
from alembic import op
import sqlalchemy as sa

revision = 'd41234567890'
down_revision = 'c30123456789'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sources', sa.Column('research_enabled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM sources WHERE research_enabled_at IS NOT NULL)')):
        raise RuntimeError('机构接入时间已有记录，拒绝删除历史时间边界')
    op.drop_column('sources', 'research_enabled_at')
