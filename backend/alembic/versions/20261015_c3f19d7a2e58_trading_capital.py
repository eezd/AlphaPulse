"""trading capital

Revision ID: c3f19d7a2e58
Revises: a54081406f23
Create Date: 2026-10-15 10:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f19d7a2e58'
down_revision: Union[str, Sequence[str], None] = 'a54081406f23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 用户要求只用设定的资金操盘，不动用账户全部余额（2026-10-15）
    op.add_column('trading_account', sa.Column('capital_usdt', sa.Numeric(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trading_account', 'capital_usdt')
