"""Retire CLS sources after explicitly authorized operator cleanup."""
from alembic import op
import sqlalchemy as sa

revision = 'c91234567890'
down_revision = 'b81234567890'
branch_labels = None
depends_on = None


def upgrade():
    # Existing foreign keys reject retirement until source data is removed.
    op.execute(sa.text("DELETE FROM sources WHERE code IN ('cls_depth', 'cls_telegraph')"))


def downgrade():
    raise RuntimeError('财联社退役及已删除的数据不能通过降级恢复')
