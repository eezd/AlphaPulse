"""Remove the retired shared phrase vocabulary.

Package keywords and historical attempt/version rule snapshots remain intact.
Downgrade recreates an empty table; removed vocabulary entries are not restored.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "ef6789012345"
down_revision = "de5678901234"
branch_labels = depends_on = None


def upgrade():
    op.drop_table("news_research_vocabulary_versions")


def downgrade():
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
