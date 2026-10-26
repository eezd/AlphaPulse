"""新增国外新闻和宏观公告来源。

Revision ID: e6f8a2c4d109
Revises: d2a4c6e8f103
"""

from alembic import op
import sqlalchemy as sa

revision = "e6f8a2c4d109"
down_revision = "d2a4c6e8f103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sources = sa.table(
        "sources",
        sa.column("code", sa.String), sa.column("name", sa.String), sa.column("list_url", sa.String),
        sa.column("enabled", sa.Boolean), sa.column("interval_seconds", sa.Integer),
        sa.column("backfill_hours", sa.Integer), sa.column("consecutive_failures", sa.Integer),
    )
    op.bulk_insert(sources, [
        {"code": code, "name": name, "list_url": url, "enabled": enabled,
         "interval_seconds": 60, "backfill_hours": 72, "consecutive_failures": 0}
        for code, name, url, enabled in [
            ("coindesk", "CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", True),
            ("fed", "美联储·政策与讲话", "https://www.federalreserve.gov/feeds/feeds.htm", True),
            ("bls", "美国劳工统计局·CPI/PPI/就业", "https://www.bls.gov/feed/", True),
            ("bea", "美国经济分析局", "https://apps.bea.gov/rss/rss.xml", True),
        ]
    ])


def downgrade() -> None:
    # 已有采集记录时由外键拒绝回退，不级联删除新闻、原始响应或日志。
    op.execute("DELETE FROM sources WHERE code IN ('coindesk', 'fed', 'bls', 'bea')")
