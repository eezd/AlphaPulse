"""Add isolated three-stage news research without clearing business data."""
from alembic import op
revision = 'b2c4d6e8f012'
down_revision = 'a8b1cd465ef3'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE skill_nodes ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'shared'")
    op.execute("ALTER TABLE risk_settings ADD COLUMN second_provider_id INTEGER REFERENCES ai_providers(id), ADD COLUMN second_model VARCHAR(128) NOT NULL DEFAULT '', ADD COLUMN grouping_skill_id INTEGER REFERENCES skill_nodes(id), ADD COLUMN analysis_skill_id INTEGER REFERENCES skill_nodes(id), ADD COLUMN outlook_skill_id INTEGER REFERENCES skill_nodes(id), ADD COLUMN start_at TIMESTAMPTZ")
    op.execute('UPDATE risk_settings SET enabled = false')
    op.execute("""
CREATE TABLE news_ai_article_analyses (
	id BIGSERIAL NOT NULL,
	news_id BIGINT NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	state VARCHAR(16) NOT NULL,
	input JSONB NOT NULL,
	result JSONB,
	rules JSONB,
	model VARCHAR(128),
	usage JSONB,
	error TEXT,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (news_id, content_hash),
	FOREIGN KEY(news_id) REFERENCES news_items (id)
)

""")
    op.execute('CREATE INDEX ix_news_ai_article_analyses_state ON news_ai_article_analyses (state)')
    op.execute('CREATE INDEX ix_news_ai_article_analyses_news_id ON news_ai_article_analyses (news_id)')
    op.execute("""
CREATE TABLE news_ai_summaries (
	id BIGSERIAL NOT NULL,
	title TEXT NOT NULL,
	category VARCHAR(64) NOT NULL,
	entities JSONB NOT NULL,
	stars INTEGER NOT NULL,
	peak_stars INTEGER NOT NULL,
	result JSONB,
	published_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	changed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	dirty_since TIMESTAMP WITH TIME ZONE,
	due_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revision INTEGER NOT NULL,
	published_revision INTEGER NOT NULL,
	state VARCHAR(16) NOT NULL,
	error TEXT,
	analyzed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
)

""")
    op.execute('CREATE INDEX ix_news_ai_summaries_due_at ON news_ai_summaries (due_at)')
    op.execute('CREATE INDEX ix_news_ai_summaries_updated_at ON news_ai_summaries (updated_at)')
    op.execute("""
CREATE TABLE news_ai_summary_articles (
	id BIGSERIAL NOT NULL,
	summary_id BIGINT NOT NULL,
	analysis_id BIGINT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (summary_id, analysis_id),
	FOREIGN KEY(summary_id) REFERENCES news_ai_summaries (id),
	FOREIGN KEY(analysis_id) REFERENCES news_ai_article_analyses (id)
)

""")
    op.execute('CREATE INDEX ix_news_ai_summary_articles_analysis_id ON news_ai_summary_articles (analysis_id)')
    op.execute('CREATE INDEX ix_news_ai_summary_articles_summary_id ON news_ai_summary_articles (summary_id)')
    op.execute("""
CREATE TABLE news_ai_summary_versions (
	id BIGSERIAL NOT NULL,
	summary_id BIGINT NOT NULL,
	revision INTEGER NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	state VARCHAR(16) NOT NULL,
	input JSONB NOT NULL,
	result JSONB,
	rules JSONB NOT NULL,
	model VARCHAR(128) NOT NULL,
	usage JSONB,
	error TEXT,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (summary_id, revision, fingerprint),
	FOREIGN KEY(summary_id) REFERENCES news_ai_summaries (id)
)

""")
    op.execute('CREATE INDEX ix_news_ai_summary_versions_summary_id ON news_ai_summary_versions (summary_id)')

    op.execute("ALTER TABLE risk_settings ADD COLUMN third_provider_id INTEGER REFERENCES ai_providers(id), ADD COLUMN third_model VARCHAR(128) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE news_ai_summaries ADD COLUMN importance_stars INTEGER NOT NULL DEFAULT 1, ADD COLUMN tags JSONB NOT NULL DEFAULT '[]', ADD COLUMN content_kind VARCHAR(32) NOT NULL DEFAULT 'report', ADD COLUMN fact TEXT NOT NULL DEFAULT ''")
    op.execute('CREATE INDEX ix_news_ai_summaries_importance ON news_ai_summaries (importance_stars)')
    op.execute('CREATE INDEX ix_news_ai_summaries_tags ON news_ai_summaries USING gin(tags)')
    op.execute("ALTER TABLE sources ADD COLUMN adapter VARCHAR(16) NOT NULL DEFAULT 'builtin'")
    op.execute("CREATE TABLE news_package_evidence (id BIGSERIAL PRIMARY KEY, summary_id BIGINT NOT NULL REFERENCES news_ai_summaries(id), version_id BIGINT NOT NULL REFERENCES news_ai_summary_versions(id), kind VARCHAR(32) NOT NULL, reference TEXT NOT NULL, data JSONB NOT NULL, fetched_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    op.execute('CREATE INDEX ix_news_package_evidence_summary_id ON news_package_evidence(summary_id)')
    op.execute('CREATE INDEX ix_news_package_evidence_version_id ON news_package_evidence(version_id)')


def downgrade():
    op.drop_table('news_package_evidence')
    op.drop_column('sources', 'adapter')
    for table in ('news_ai_summary_versions', 'news_ai_summary_articles', 'news_ai_summaries', 'news_ai_article_analyses'):
        op.drop_table(table)
    for column in ('third_provider_id', 'third_model', 'start_at', 'outlook_skill_id', 'analysis_skill_id', 'grouping_skill_id', 'second_model', 'second_provider_id'):
        op.drop_column('risk_settings', column)
    op.drop_column('skill_nodes', 'scope')
