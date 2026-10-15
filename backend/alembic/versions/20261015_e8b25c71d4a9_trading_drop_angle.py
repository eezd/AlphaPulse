"""trading drop angle

Revision ID: e8b25c71d4a9
Revises: c3f19d7a2e58
Create Date: 2026-10-15 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8b25c71d4a9'
down_revision: Union[str, Sequence[str], None] = 'c3f19d7a2e58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 用户决定模拟盘只做短线（2026-10-15），不再有角度之分
    op.drop_column('trading_symbols', 'angle')
    op.drop_column('trading_analyses', 'angle')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('trading_analyses', sa.Column('angle', sa.String(length=8), nullable=False, server_default='short'))
    op.add_column('trading_symbols', sa.Column('angle', sa.String(length=8), nullable=False, server_default='short'))
