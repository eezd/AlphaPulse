"""Add disabled US official collectors and resumable provenance metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'da1234567890'
down_revision = 'c91234567890'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sources', sa.Column('history_cursor', JSONB, nullable=False, server_default='{}'))
    op.add_column('crawl_runs', sa.Column('stats', JSONB, nullable=False, server_default='{}'))
    op.add_column('news_revisions', sa.Column('extra', JSONB, nullable=True))
    table = sa.table('sources', *(sa.column(k) for k in ('code', 'name', 'list_url', 'adapter', 'collector', 'enabled', 'interval_seconds', 'backfill_hours', 'consecutive_failures')))
    op.bulk_insert(table, [{'code': 'sec', 'name': 'SEC·证券监管', 'list_url': 'https://www.sec.gov/newsroom/press-releases', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'cftc', 'name': 'CFTC·衍生品监管', 'list_url': 'https://www.cftc.gov/PressRoom/PressReleases', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'treasury', 'name': '美国财政部', 'list_url': 'https://home.treasury.gov/news/press-releases', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'ofac', 'name': 'OFAC·金融制裁', 'list_url': 'https://ofac.treasury.gov/recent-actions', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'fincen', 'name': 'FinCEN·反洗钱', 'list_url': 'https://www.fincen.gov/news/press-releases', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'whitehouse', 'name': '白宫·政策', 'list_url': 'https://www.whitehouse.gov/presidential-actions/', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'congress', 'name': '美国国会·立法', 'list_url': 'https://api.congress.gov/v3/bill?format=json&limit=250', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 900, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'federal_register', 'name': 'Federal Register·规则', 'list_url': 'https://www.federalregister.gov/api/v1/documents.json', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 900, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'occ', 'name': 'OCC·银行与稳定币', 'list_url': 'https://www.occ.gov/news-events/newsroom/news-issuances-by-year/news-releases/2026-news-releases.html', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}, {'code': 'doj', 'name': 'DOJ·加密机构执法', 'list_url': 'https://www.justice.gov/api/v1/press_releases.json?pagesize=50&sort=date&direction=DESC&fields=uuid,date', 'adapter': 'builtin', 'collector': 'news', 'enabled': False, 'interval_seconds': 300, 'backfill_hours': 72, 'consecutive_failures': 0}])


def downgrade():
    # Foreign keys intentionally reject removal of sources with collected data.
    op.execute("DELETE FROM sources WHERE code IN ('sec','cftc','treasury','ofac','fincen','whitehouse','congress','federal_register','occ','doj')")
    op.drop_column('news_revisions', 'extra')
    op.drop_column('crawl_runs', 'stats')
    op.drop_column('sources', 'history_cursor')
