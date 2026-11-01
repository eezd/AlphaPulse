"""Add the news pipeline concurrency limit and durable item states."""
from alembic import op
import sqlalchemy as sa

revision = 'e5f701234567'
down_revision = 'd4e6f8012345'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('risk_settings', sa.Column('max_concurrency', sa.Integer(), server_default='5', nullable=False))
    op.create_check_constraint('risk_concurrency_positive', 'risk_settings', 'max_concurrency > 0')
    op.execute("""UPDATE news_ai_reprocess_jobs j SET items = (
        SELECT COALESCE(jsonb_agg(item || jsonb_build_object('state', CASE
            WHEN ordinal > j.position THEN 'pending'
            WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(j.failures) f
                WHERE f->>'stage' = 'grouping' AND f->>'id' = item->>'id') THEN 'failed'
            ELSE 'done' END) ORDER BY ordinal), '[]'::jsonb)
        FROM jsonb_array_elements(j.items) WITH ORDINALITY AS entries(item, ordinal))""")


def downgrade():
    op.execute("""UPDATE news_ai_reprocess_jobs SET items = (
        SELECT COALESCE(jsonb_agg(item - 'state' ORDER BY CASE WHEN item->>'state' IN ('done', 'failed') THEN 0 ELSE 1 END, ordinal), '[]'::jsonb)
        FROM jsonb_array_elements(items) WITH ORDINALITY AS entries(item, ordinal))""")
    op.drop_constraint('risk_concurrency_positive', 'risk_settings', type_='check')
    op.drop_column('risk_settings', 'max_concurrency')
