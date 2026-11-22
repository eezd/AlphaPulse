"""接入仅采集的公开研究文章来源，保留既有来源与业务历史。"""
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa

revision = 'c30123456789'
down_revision = 'b29012345678'
branch_labels = None
depends_on = None

# 迁移固定其发布时的验证结果，不随运行目录的后续修改变化。
SOURCES = (
    ('cls_depth', '财联社文章', 'https://www.cls.cn/depth', False),
    ('citi_insights', 'Citi', 'https://www.citigroup.com/global/insights/all', True),
    ('jpmorgan_research', 'J.P. Morgan', 'https://www.jpmorgan.com/insights/research', True),
    ('blackrock_insights', 'BlackRock', 'https://www.blackrock.com/corporate/insights/blackrock-investment-institute', False),
    ('goldman_insights', 'Goldman Sachs', 'https://www.goldmansachs.com/insights', False),
    ('bloomberg_articles', 'Bloomberg', 'https://www.bloomberg.com/markets', False),
)


def upgrade():
    connection = op.get_bind()
    for code, _, url, _ in SOURCES:
        existing = connection.execute(sa.text('SELECT list_url FROM sources WHERE code=:code'), {'code': code}).scalar()
        if existing is not None and urlsplit(existing).hostname != urlsplit(url).hostname:
            raise RuntimeError(f'研究来源 {code} 与现有来源网站冲突')
    op.add_column('sources', sa.Column('analysis_policy', sa.String(16), nullable=False, server_default='automatic'))
    op.create_check_constraint('ck_sources_analysis_policy', 'sources', "analysis_policy IN ('automatic', 'collect_only')")
    for code, name, url, enabled in SOURCES:
        connection.execute(sa.text('''INSERT INTO sources
            (code,name,list_url,adapter,collector,enabled,interval_seconds,backfill_hours,
             consecutive_failures,history_target,analysis_policy)
            VALUES (:code,:name,:url,'builtin','news',:enabled,21600,720,0,now()-interval '30 days','collect_only')
            ON CONFLICT (code) DO UPDATE SET analysis_policy='collect_only' '''),
            {'code': code, 'name': name, 'url': url, 'enabled': enabled})


def downgrade():
    raise RuntimeError('研究文章已经入库后不自动删除来源或资料；请准备保留数据的迁移')
