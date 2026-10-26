"""日历统计期、发布阶段及当前官方历史序列。

Revision ID: a8c1d4e7f230
Revises: f7a9b3d5e210
"""
from alembic import op
import sqlalchemy as sa

revision = "a8c1d4e7f230"
down_revision = "f7a9b3d5e210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("economic_releases", sa.Column("period", sa.String(10)))
    op.add_column("economic_releases", sa.Column("stage", sa.String(16)))
    op.add_column("sources", sa.Column("collector", sa.String(16), nullable=False, server_default="news"))
    sources = sa.table("sources", *[sa.column(k, t) for k, t in [
        ("code", sa.String), ("name", sa.String), ("list_url", sa.String), ("collector", sa.String),
        ("enabled", sa.Boolean), ("interval_seconds", sa.Integer), ("backfill_hours", sa.Integer),
        ("consecutive_failures", sa.Integer),
    ]])
    op.bulk_insert(sources, [{"code": "umich", "name": "密歇根大学·经济日历", "collector": "calendar",
                             "list_url": "https://www.sca.isr.umich.edu/", "enabled": True,
                             "interval_seconds": 3600, "backfill_hours": 0, "consecutive_failures": 0}])
    op.create_table(
        "economic_series_values",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("indicator", sa.String(48), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("value", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_response_id", sa.BigInteger, sa.ForeignKey("raw_responses.id"), nullable=False),
        sa.UniqueConstraint("source", "indicator", "period", "stage"),
    )


def downgrade() -> None:
    # 已有原始响应时外键拒绝删除来源，不级联删除用户数据。
    op.execute("DELETE FROM sources WHERE code = 'umich'")
    op.drop_table("economic_series_values")
    op.drop_column("sources", "collector")
    op.drop_column("economic_releases", "stage")
    op.drop_column("economic_releases", "period")
