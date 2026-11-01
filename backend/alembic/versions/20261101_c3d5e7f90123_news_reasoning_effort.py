"""Add independent reasoning effort overrides for news stages."""
from alembic import op
import sqlalchemy as sa

revision = 'c3d5e7f90123'
down_revision = 'b2c4d6e8f012'
branch_labels = None
depends_on = None


def upgrade():
    for column in ('reasoning_effort', 'second_reasoning_effort', 'third_reasoning_effort'):
        op.add_column('risk_settings', sa.Column(column, sa.String(16), nullable=True))


def downgrade():
    for column in ('third_reasoning_effort', 'second_reasoning_effort', 'reasoning_effort'):
        op.drop_column('risk_settings', column)
