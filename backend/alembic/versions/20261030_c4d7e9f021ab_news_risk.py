"""Add independent news risk records; preserve existing data."""
from alembic import op
revision = 'c4d7e9f021ab'
down_revision = 'b9d2e5f8a341'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE risk_settings (
	id SERIAL NOT NULL,
	enabled BOOLEAN NOT NULL,
	provider_id INTEGER,
	model VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(provider_id) REFERENCES ai_providers (id)
)
    """)
    op.execute("""
CREATE TABLE risk_events (
	id BIGSERIAL NOT NULL,
	title TEXT NOT NULL,
	category VARCHAR(64) NOT NULL,
	entities JSONB NOT NULL,
	stars INTEGER NOT NULL,
	peak_stars INTEGER NOT NULL,
	result JSONB NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)
    """)
    op.execute('CREATE INDEX ix_risk_events_published_at ON risk_events (published_at)')
    op.execute('CREATE INDEX ix_risk_events_updated_at ON risk_events (updated_at)')
    op.execute("""
CREATE TABLE risk_analyses (
	id BIGSERIAL NOT NULL,
	news_id BIGINT NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	state VARCHAR(16) NOT NULL,
	event_id BIGINT,
	input JSONB NOT NULL,
	result JSONB,
	model VARCHAR(128),
	usage JSONB,
	error TEXT,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (news_id, content_hash),
	FOREIGN KEY(news_id) REFERENCES news_items (id),
	FOREIGN KEY(event_id) REFERENCES risk_events (id)
)
    """)
    op.execute('CREATE INDEX ix_risk_analyses_event_id ON risk_analyses (event_id)')
    op.execute('CREATE INDEX ix_risk_analyses_news_id ON risk_analyses (news_id)')
    op.execute('CREATE INDEX ix_risk_analyses_state ON risk_analyses (state)')
    op.execute("""
CREATE TABLE risk_snapshots (
	id BIGSERIAL NOT NULL,
	state VARCHAR(16) NOT NULL,
	result JSONB,
	quotes JSONB NOT NULL,
	model VARCHAR(128) NOT NULL,
	error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
)
    """)


def downgrade():
    op.drop_table('risk_snapshots')
    op.drop_table('risk_analyses')
    op.drop_table('risk_events')
    op.drop_table('risk_settings')
