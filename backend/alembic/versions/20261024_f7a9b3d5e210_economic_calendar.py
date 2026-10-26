"""美国经济日历与原始公布修订。

Revision ID: f7a9b3d5e210
Revises: e6f8a2c4d109
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f7a9b3d5e210"
down_revision = "e6f8a2c4d109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "economic_releases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(160), nullable=False),
        sa.Column("family", sa.String(16), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_status", sa.String(16), nullable=False),
        sa.Column("schedule_url", sa.Text, nullable=False),
        sa.Column("release_url", sa.Text),
        sa.Column("schedule_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("values", postgresql.JSONB, nullable=False),
        sa.Column("revision_no", sa.Integer, nullable=False),
        sa.UniqueConstraint("source", "external_id"),
    )
    op.create_index("ix_economic_releases_scheduled_at", "economic_releases", ["scheduled_at"])
    op.create_index("ix_economic_releases_next_check_at", "economic_releases", ["next_check_at"])
    op.create_table(
        "economic_revisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("release_id", sa.Integer, sa.ForeignKey("economic_releases.id"), nullable=False),
        sa.Column("revision_no", sa.Integer, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("raw_response_id", sa.BigInteger, sa.ForeignKey("raw_responses.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("release_id", "revision_no"),
    )
    op.create_index("ix_economic_revisions_release_id", "economic_revisions", ["release_id"])


def downgrade() -> None:
    op.drop_table("economic_revisions")
    op.drop_table("economic_releases")
