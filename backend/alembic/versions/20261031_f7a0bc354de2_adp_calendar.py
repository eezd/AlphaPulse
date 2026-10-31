"""Register the official monthly ADP calendar source."""
from alembic import op
import sqlalchemy as sa

revision = "f7a0bc354de2"
down_revision = "e6f9ab243cd1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    source = sa.table("sources", *[sa.column(key, kind) for key, kind in [
        ("code", sa.String), ("name", sa.String), ("list_url", sa.String), ("collector", sa.String),
        ("enabled", sa.Boolean), ("interval_seconds", sa.Integer), ("backfill_hours", sa.Integer),
        ("consecutive_failures", sa.Integer),
    ]])
    op.bulk_insert(source, [{"code": "adp", "name": "ADP·月度私营就业报告",
                             "list_url": "https://mediacenter.adp.com/press-releases?keywords=National+Employment+Report",
                             "collector": "calendar", "enabled": True, "interval_seconds": 3600,
                             "backfill_hours": 72, "consecutive_failures": 0}])


def downgrade() -> None:
    # Existing raw responses prevent source deletion through their foreign key.
    op.execute("DELETE FROM sources WHERE code = 'adp'")
