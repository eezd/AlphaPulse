"""default um symbols

Revision ID: bf0ffbfcaf9a
Revises: c7e1beb72c75
Create Date: 2026-10-13 06:01:27.204671

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bf0ffbfcaf9a'
down_revision: Union[str, Sequence[str], None] = 'c7e1beb72c75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # U 本位永续的默认交易对；精度取自 2026-10-13 币安 fapi exchangeInfo（tickSize 0.10/0.01/0.010，stepSize 0.001/0.001/0.01）
    op.execute(sa.text("""
        INSERT INTO market_symbols (market, symbol, base, quote, price_precision, volume_precision, status, is_default)
        VALUES ('um', 'BTCUSDT', 'BTC', 'USDT', 1, 3, 'pending', true),
               ('um', 'ETHUSDT', 'ETH', 'USDT', 2, 3, 'pending', true),
               ('um', 'BNBUSDT', 'BNB', 'USDT', 2, 2, 'pending', true)
        ON CONFLICT (market, symbol) DO UPDATE SET is_default = true
    """))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("UPDATE market_symbols SET is_default = false WHERE market = 'um' AND symbol IN ('BTCUSDT', 'ETHUSDT', 'BNBUSDT')"))
