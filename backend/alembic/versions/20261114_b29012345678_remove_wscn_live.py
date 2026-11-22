"""Retire WSCN flash after explicitly authorized operator cleanup."""
from alembic import op
import sqlalchemy as sa

revision = 'b29012345678'
down_revision = 'a18901234567'
branch_labels = None
depends_on = None


def upgrade():
    # 外键拒绝尚未经过操作员定向清理的现存实例。
    op.execute(sa.text("DELETE FROM sources WHERE code='wscn_live'"))


def downgrade():
    raise RuntimeError('华尔街见闻快讯退役及已删除的数据不能通过降级恢复')
