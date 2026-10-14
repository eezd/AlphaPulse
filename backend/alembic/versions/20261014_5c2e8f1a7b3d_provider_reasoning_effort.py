"""provider reasoning effort replaces temperature

Revision ID: 5c2e8f1a7b3d
Revises: 8349ae014ddd
Create Date: 2026-10-14 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c2e8f1a7b3d'
down_revision: Union[str, Sequence[str], None] = '8349ae014ddd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 用户决定不再设置 temperature（2026-10-14），改为思考强度
    op.drop_column('ai_providers', 'temperature')
    op.add_column('ai_providers', sa.Column('reasoning_effort', sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ai_providers', 'reasoning_effort')
    op.add_column('ai_providers', sa.Column('temperature', sa.Float(), nullable=True))
