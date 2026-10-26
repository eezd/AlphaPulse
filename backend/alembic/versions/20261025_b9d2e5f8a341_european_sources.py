"""欧洲官方新闻与经济日历来源。

Revision ID: b9d2e5f8a341
Revises: a8c1d4e7f230
"""
from alembic import op
import sqlalchemy as sa

revision = "b9d2e5f8a341"
down_revision = "a8c1d4e7f230"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sources = sa.table("sources", *[sa.column(k, t) for k, t in [
        ("code", sa.String), ("name", sa.String), ("list_url", sa.String), ("collector", sa.String),
        ("enabled", sa.Boolean), ("interval_seconds", sa.Integer), ("backfill_hours", sa.Integer),
        ("consecutive_failures", sa.Integer),
    ]])
    op.bulk_insert(sources, [
        {"code": code, "name": name, "list_url": url, "collector": "news", "enabled": True,
         "interval_seconds": 60, "backfill_hours": 72, "consecutive_failures": 0}
        for code, name, url in [
            ("ecb", "欧洲央行·政策与讲话", "https://www.ecb.europa.eu/home/html/rss.en.html"),
            ("eurostat", "Eurostat·欧元区统计", "https://ec.europa.eu/eurostat/news/euro-indicators"),
            ("boe", "英国央行·政策与讲话", "https://www.bankofengland.co.uk/rss"),
            ("ons", "英国国家统计局", "https://www.ons.gov.uk/releasecalendar"),
            ("destatis", "德国联邦统计局", "https://www.destatis.de/DE/Presse/_inhalt.html"),
            ("insee", "法国国家统计局", "https://www.insee.fr/fr/statistiques?categorie=10"),
        ]
    ])


def downgrade() -> None:
    # 已有采集记录时由外键拒绝删除，不级联清理新闻或原始响应。
    op.execute("DELETE FROM sources WHERE code IN ('ecb','eurostat','boe','ons','destatis','insee')")
