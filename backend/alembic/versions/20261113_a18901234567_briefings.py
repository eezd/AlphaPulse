"""单篇新闻分析与报表新增存储；保留旧研究和原文数据。"""
from alembic import op

revision = 'a18901234567'
down_revision = 'f07890123456'
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE TABLE briefing_settings (\n\tid SERIAL NOT NULL, \n\tenabled BOOLEAN DEFAULT 'false' NOT NULL, \n\tcontrol_revision INTEGER DEFAULT '0' NOT NULL, \n\tconfig JSONB DEFAULT '{}' NOT NULL, \n\tenabled_at TIMESTAMP WITH TIME ZONE, \n\thistory_cursor DATE, \n\thistory_start DATE, \n\thistory_end DATE, \n\thistory_enrolled BOOLEAN DEFAULT 'false' NOT NULL, \n\ttelegram_token TEXT, \n\ttelegram_chat_id VARCHAR(100), \n\ttelegram_enabled BOOLEAN DEFAULT 'false' NOT NULL, \n\tPRIMARY KEY (id)\n)")
    op.execute("CREATE TABLE briefing_analyses (\n\tid BIGSERIAL NOT NULL, \n\tnews_id BIGINT NOT NULL, \n\tversion_no INTEGER NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tlane VARCHAR(24) NOT NULL, \n\tcontrol_revision INTEGER, \n\tcontent_hash VARCHAR(64), \n\tsource_revision INTEGER, \n\teffective_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\ttime_basis VARCHAR(24) NOT NULL, \n\tinput JSONB, \n\trules JSONB, \n\toutput JSONB, \n\tkeywords JSONB DEFAULT '[]' NOT NULL, \n\timportance_score INTEGER, \n\timportance_stars INTEGER, \n\tusage JSONB, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_briefing_analysis_version UNIQUE (news_id, version_no), \n\tFOREIGN KEY(news_id) REFERENCES news_items (id)\n)")
    op.execute('CREATE INDEX ix_briefing_analyses_effective_at ON briefing_analyses (effective_at)')
    op.execute('CREATE INDEX ix_briefing_analyses_news_id ON briefing_analyses (news_id)')
    op.execute('CREATE INDEX ix_briefing_analysis_keywords ON briefing_analyses USING gin (keywords)')
    op.execute('CREATE INDEX ix_briefing_analysis_queue ON briefing_analyses (state, lane, id)')
    op.execute("CREATE UNIQUE INDEX uq_briefing_analysis_active ON briefing_analyses (news_id) WHERE state IN ('pending', 'processing')")
    op.execute("CREATE TABLE briefing_reports (\n\tid BIGSERIAL NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n\treport_date DATE NOT NULL, \n\tversion_no INTEGER NOT NULL, \n\trequest_id VARCHAR(36), \n\tregenerate BOOLEAN DEFAULT 'false' NOT NULL, \n\tlane VARCHAR(24) NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tcontrol_revision INTEGER, \n\twindow_start TIMESTAMP WITH TIME ZONE NOT NULL, \n\twindow_end TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdue_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tnext_check_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfirst_check_at TIMESTAMP WITH TIME ZONE, \n\tcutoff_at TIMESTAMP WITH TIME ZONE, \n\tinput JSONB, \n\tcoverage JSONB, \n\tmarket JSONB, \n\trules JSONB, \n\toutput JSONB, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_briefing_report_version UNIQUE (kind, report_date, version_no), \n\tCONSTRAINT uq_briefing_report_request UNIQUE (request_id)\n)")
    op.execute('CREATE INDEX ix_briefing_report_queue ON briefing_reports (state, next_check_at, lane)')
    op.execute("CREATE UNIQUE INDEX uq_briefing_report_active ON briefing_reports (kind, report_date) WHERE state IN ('pending', 'waiting_backfill', 'waiting_coverage', 'processing')")
    op.execute('CREATE TABLE briefing_report_parts (\n\tid BIGSERIAL NOT NULL, \n\treport_id BIGINT NOT NULL, \n\tpart_key VARCHAR(80) NOT NULL, \n\tinput_hash VARCHAR(64) NOT NULL, \n\tstage VARCHAR(32) NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tcontrol_revision INTEGER NOT NULL, \n\tinput JSONB NOT NULL, \n\trules JSONB NOT NULL, \n\toutput JSONB, \n\tusage JSONB, \n\treused_from BIGINT, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_briefing_report_part UNIQUE (report_id, part_key), \n\tFOREIGN KEY(report_id) REFERENCES briefing_reports (id), \n\tFOREIGN KEY(reused_from) REFERENCES briefing_report_parts (id)\n)')
    op.execute('CREATE INDEX ix_briefing_part_fingerprint ON briefing_report_parts (input_hash, state)')
    op.execute('CREATE INDEX ix_briefing_report_parts_report_id ON briefing_report_parts (report_id)')
    op.execute('CREATE TABLE briefing_notifications (\n\tid BIGSERIAL NOT NULL, \n\tincident VARCHAR(64) NOT NULL, \n\tdedup_key VARCHAR(160) NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tmessage TEXT NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tnext_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tsent_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (dedup_key)\n)')
    op.execute('CREATE INDEX ix_briefing_notifications_state ON briefing_notifications (state)')
    op.execute("INSERT INTO briefing_settings (id, enabled, control_revision, config, history_enrolled, telegram_enabled) VALUES (1, false, 0, '{}'::jsonb, false, false)")

def downgrade():
    raise RuntimeError('报表审计数据不能通过自动降级删除；请保留新增表并回滚应用')
