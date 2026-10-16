"""trading per symbol engine

Revision ID: a7d3e5f20c18
Revises: f2a6c8d91b47
Create Date: 2026-10-16 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3e5f20c18'
down_revision: Union[str, Sequence[str], None] = 'f2a6c8d91b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 用户决定启停和交易资金按币种分开（2026-10-16）：原账户资金复制给每个币，全部停止
    op.add_column('trading_symbols', sa.Column('running', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('trading_symbols', sa.Column('running_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trading_symbols', sa.Column('capital_usdt', sa.Numeric(), nullable=True))
    op.execute("UPDATE trading_symbols SET capital_usdt = (SELECT capital_usdt FROM trading_account WHERE id = 1)")
    op.drop_column('trading_account', 'running')
    op.drop_column('trading_account', 'running_changed_at')
    op.drop_column('trading_account', 'capital_usdt')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('trading_account', sa.Column('capital_usdt', sa.Numeric(), nullable=True))
    op.add_column('trading_account', sa.Column('running_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trading_account', sa.Column('running', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE trading_account SET capital_usdt = (SELECT max(capital_usdt) FROM trading_symbols)")
    op.drop_column('trading_symbols', 'capital_usdt')
    op.drop_column('trading_symbols', 'running_changed_at')
    op.drop_column('trading_symbols', 'running')
