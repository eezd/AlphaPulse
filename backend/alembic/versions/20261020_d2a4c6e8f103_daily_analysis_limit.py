"""每日分析上限改为用户配置。

Revision ID: d2a4c6e8f103
Revises: b8d1f4a92c60
"""

from alembic import op
import sqlalchemy as sa

revision = "d2a4c6e8f103"
down_revision = "b8d1f4a92c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trading_account", sa.Column("daily_analysis_limit", sa.Integer(), nullable=False, server_default="60"))


def downgrade() -> None:
    op.drop_column("trading_account", "daily_analysis_limit")
