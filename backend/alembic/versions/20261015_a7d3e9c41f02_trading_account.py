"""trading account

Revision ID: a7d3e9c41f02
Revises: 5c2e8f1a7b3d
Create Date: 2026-10-15 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3e9c41f02'
down_revision: Union[str, Sequence[str], None] = '5c2e8f1a7b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('trading_account',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('api_key_encrypted', sa.LargeBinary(), nullable=True),
    sa.Column('api_secret_encrypted', sa.LargeBinary(), nullable=True),
    sa.Column('api_key_hint', sa.String(length=8), nullable=True),
    sa.Column('running', sa.Boolean(), nullable=False),
    sa.Column('running_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('provider_id', sa.Integer(), nullable=True),
    sa.Column('reasoning_effort', sa.String(length=8), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('id = 1', name='trading_account_single_row'),
    sa.ForeignKeyConstraint(['provider_id'], ['ai_providers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('trading_account')
