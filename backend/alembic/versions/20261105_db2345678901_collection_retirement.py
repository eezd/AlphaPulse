"""Persist bounded calendar collection failures."""
from alembic import op
import sqlalchemy as sa

revision = "db2345678901"
down_revision = "da1234567890"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("economic_releases", sa.Column("collection_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("economic_releases", sa.Column("collection_retired_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("economic_releases", "collection_retired_at")
    op.drop_column("economic_releases", "collection_failures")
