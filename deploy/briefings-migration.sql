BEGIN;

-- Running upgrade f07890123456 -> a18901234567

CREATE TABLE briefing_settings (
    id SERIAL NOT NULL, 
    enabled BOOLEAN DEFAULT 'false' NOT NULL, 
    control_revision INTEGER DEFAULT '0' NOT NULL, 
    config JSONB DEFAULT '{}' NOT NULL, 
    enabled_at TIMESTAMP WITH TIME ZONE, 
    history_cursor DATE, 
    history_start DATE, 
    history_end DATE, 
    history_enrolled BOOLEAN DEFAULT 'false' NOT NULL, 
    telegram_token TEXT, 
    telegram_chat_id VARCHAR(100), 
    telegram_enabled BOOLEAN DEFAULT 'false' NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE briefing_analyses (
    id BIGSERIAL NOT NULL, 
    news_id BIGINT NOT NULL, 
    version_no INTEGER NOT NULL, 
    state VARCHAR(24) NOT NULL, 
    lane VARCHAR(24) NOT NULL, 
    control_revision INTEGER, 
    content_hash VARCHAR(64), 
    source_revision INTEGER, 
    effective_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    time_basis VARCHAR(24) NOT NULL, 
    input JSONB, 
    rules JSONB, 
    output JSONB, 
    keywords JSONB DEFAULT '[]' NOT NULL, 
    importance_score INTEGER, 
    importance_stars INTEGER, 
    usage JSONB, 
    error TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_briefing_analysis_version UNIQUE (news_id, version_no), 
    FOREIGN KEY(news_id) REFERENCES news_items (id)
);

CREATE INDEX ix_briefing_analyses_effective_at ON briefing_analyses (effective_at);

CREATE INDEX ix_briefing_analyses_news_id ON briefing_analyses (news_id);

CREATE INDEX ix_briefing_analysis_keywords ON briefing_analyses USING gin (keywords);

CREATE INDEX ix_briefing_analysis_queue ON briefing_analyses (state, lane, id);

CREATE UNIQUE INDEX uq_briefing_analysis_active ON briefing_analyses (news_id) WHERE state IN ('pending', 'processing');

CREATE TABLE briefing_reports (
    id BIGSERIAL NOT NULL, 
    kind VARCHAR(16) NOT NULL, 
    report_date DATE NOT NULL, 
    version_no INTEGER NOT NULL, 
    request_id VARCHAR(36), 
    regenerate BOOLEAN DEFAULT 'false' NOT NULL, 
    lane VARCHAR(24) NOT NULL, 
    state VARCHAR(24) NOT NULL, 
    control_revision INTEGER, 
    window_start TIMESTAMP WITH TIME ZONE NOT NULL, 
    window_end TIMESTAMP WITH TIME ZONE NOT NULL, 
    due_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    next_check_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    first_check_at TIMESTAMP WITH TIME ZONE, 
    cutoff_at TIMESTAMP WITH TIME ZONE, 
    input JSONB, 
    coverage JSONB, 
    market JSONB, 
    rules JSONB, 
    output JSONB, 
    error TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_briefing_report_version UNIQUE (kind, report_date, version_no), 
    CONSTRAINT uq_briefing_report_request UNIQUE (request_id)
);

CREATE INDEX ix_briefing_report_queue ON briefing_reports (state, next_check_at, lane);

CREATE UNIQUE INDEX uq_briefing_report_active ON briefing_reports (kind, report_date) WHERE state IN ('pending', 'waiting_backfill', 'waiting_coverage', 'processing');

CREATE TABLE briefing_report_parts (
    id BIGSERIAL NOT NULL, 
    report_id BIGINT NOT NULL, 
    part_key VARCHAR(80) NOT NULL, 
    input_hash VARCHAR(64) NOT NULL, 
    stage VARCHAR(32) NOT NULL, 
    state VARCHAR(24) NOT NULL, 
    control_revision INTEGER NOT NULL, 
    input JSONB NOT NULL, 
    rules JSONB NOT NULL, 
    output JSONB, 
    usage JSONB, 
    reused_from BIGINT, 
    error TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_briefing_report_part UNIQUE (report_id, part_key), 
    FOREIGN KEY(report_id) REFERENCES briefing_reports (id), 
    FOREIGN KEY(reused_from) REFERENCES briefing_report_parts (id)
);

CREATE INDEX ix_briefing_part_fingerprint ON briefing_report_parts (input_hash, state);

CREATE INDEX ix_briefing_report_parts_report_id ON briefing_report_parts (report_id);

CREATE TABLE briefing_notifications (
    id BIGSERIAL NOT NULL, 
    incident VARCHAR(64) NOT NULL, 
    dedup_key VARCHAR(160) NOT NULL, 
    state VARCHAR(24) NOT NULL, 
    message TEXT NOT NULL, 
    attempts INTEGER NOT NULL, 
    next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    error TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    sent_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    UNIQUE (dedup_key)
);

CREATE INDEX ix_briefing_notifications_state ON briefing_notifications (state);

INSERT INTO briefing_settings (id, enabled, control_revision, config, history_enrolled, telegram_enabled) VALUES (1, false, 0, '{}'::jsonb, false, false);

UPDATE alembic_version SET version_num='a18901234567' WHERE alembic_version.version_num = 'f07890123456';

COMMIT;

