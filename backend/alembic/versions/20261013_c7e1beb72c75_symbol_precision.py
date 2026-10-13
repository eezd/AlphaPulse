"""symbol precision

Revision ID: c7e1beb72c75
Revises: fb4db40f2e86
Create Date: 2026-10-13 05:00:25.535575

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e1beb72c75'
down_revision: Union[str, Sequence[str], None] = 'fb4db40f2e86'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('market_symbols', sa.Column('price_precision', sa.Integer(), nullable=True))
    op.add_column('market_symbols', sa.Column('volume_precision', sa.Integer(), nullable=True))
    # 默认三个交易对的精度（2026-10-13 币安 exchangeInfo：tickSize 均为 0.01，stepSize 分别为 0.00001/0.0001/0.001）
    for symbol, volume_precision in (("BTCUSDT", 5), ("ETHUSDT", 4), ("BNBUSDT", 3)):
        op.execute(sa.text("UPDATE market_symbols SET price_precision = 2, volume_precision = :v WHERE market = 'spot' AND symbol = :s")
                   .bindparams(v=volume_precision, s=symbol))
    op.alter_column('market_symbols', 'price_precision', nullable=False)
    op.alter_column('market_symbols', 'volume_precision', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('market_symbols', 'volume_precision')
    op.drop_column('market_symbols', 'price_precision')
