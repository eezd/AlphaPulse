"""Register DOL and EIA calendar sources without enabling news crawlers."""
from alembic import op
import sqlalchemy as sa

revision = "e6f9ab243cd1"
down_revision = "d5e8fa132bc0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    source = sa.table("sources", *[sa.column(key, kind) for key, kind in [
        ("code", sa.String), ("name", sa.String), ("list_url", sa.String), ("collector", sa.String),
        ("enabled", sa.Boolean), ("interval_seconds", sa.Integer), ("backfill_hours", sa.Integer),
        ("consecutive_failures", sa.Integer),
    ]])
    op.bulk_insert(source, [{"code": code, "name": name, "list_url": url,
                             "collector": "calendar", "enabled": True, "interval_seconds": 3600,
                             "backfill_hours": 72, "consecutive_failures": 0}
                            for code, name, url in [
                                ("dol", "美国劳工部·失业金周报", "https://oui.doleta.gov/unemploy/archive.asp"),
                                ("eia", "美国能源信息署·石油库存周报", "https://www.eia.gov/petroleum/supply/weekly/schedule.php"),
                            ]])


def downgrade() -> None:
    # Existing raw responses prevent source deletion through their foreign key.
    op.execute("DELETE FROM sources WHERE code IN ('dol', 'eia')")
