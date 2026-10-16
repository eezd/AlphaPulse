"""trading fills

Revision ID: f2a6c8d91b47
Revises: e8b25c71d4a9
Create Date: 2026-10-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a6c8d91b47'
down_revision: Union[str, Sequence[str], None] = 'e8b25c71d4a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 历史仓位要包含手动成交，改由币安成交记录还原（2026-10-16）
    op.create_table('trading_fills',
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('trade_id', sa.BigInteger(), nullable=False),
    sa.Column('order_id', sa.BigInteger(), nullable=False),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('price', sa.Numeric(), nullable=False),
    sa.Column('qty', sa.Numeric(), nullable=False),
    sa.Column('realized_pnl', sa.Numeric(), nullable=False),
    sa.Column('commission', sa.Numeric(), nullable=False),
    sa.Column('commission_asset', sa.String(length=16), nullable=False),
    sa.Column('time', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('symbol', 'trade_id')
    )
    op.create_index('ix_trading_fills_symbol_time', 'trading_fills', ['symbol', 'time'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_trading_fills_symbol_time', table_name='trading_fills')
    op.drop_table('trading_fills')
