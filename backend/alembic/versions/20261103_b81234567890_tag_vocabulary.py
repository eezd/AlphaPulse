"""Add immutable reviewed vocabulary snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "b81234567890"
down_revision = "a71234567890"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "news_research_vocabulary_versions",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("entries", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_table("news_research_vocabulary_versions")
