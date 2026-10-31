"""Register the authenticated global calendar and resumable daily coverage."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a8b1cd465ef3"
down_revision = "f7a0bc354de2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("jin10_sync_days", sa.Column("day", sa.Date(), primary_key=True),
                    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("counts", postgresql.JSONB(), nullable=False))
    source = sa.table("sources", *[sa.column(key, kind) for key, kind in [
        ("code", sa.String), ("name", sa.String), ("list_url", sa.String), ("collector", sa.String),
        ("enabled", sa.Boolean), ("interval_seconds", sa.Integer), ("backfill_hours", sa.Integer),
        ("consecutive_failures", sa.Integer),
    ]])
    op.bulk_insert(source, [{"code": "jin10", "name": "金十·全球宏观日历", "list_url": "https://rili.jin10.com/",
                             "collector": "calendar", "enabled": True, "interval_seconds": 10,
                             "backfill_hours": 72, "consecutive_failures": 0}])


def downgrade() -> None:
    op.execute("DELETE FROM sources WHERE code = 'jin10'")
    op.drop_table("jin10_sync_days")
