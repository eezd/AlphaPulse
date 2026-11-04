"""Create additive two-stage research storage without modifying original or legacy tables."""

from alembic import op

revision = "a71234567890"
down_revision = "f60123456789"
branch_labels = depends_on = None


def upgrade():
    op.execute(
        "\nCREATE TABLE news_research_settings (\n\tid SERIAL NOT NULL, \n\tconfig JSONB NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tcontrol_revision INTEGER DEFAULT '0' NOT NULL, \n\tstart_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE news_research_packages (\n\tid BIGSERIAL NOT NULL, \n\tnews_id BIGINT NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\tkeywords JSONB NOT NULL, \n\terror TEXT, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (news_id), \n\tFOREIGN KEY(news_id) REFERENCES news_items (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_news_research_packages_state ON news_research_packages (state)"
    )
    op.execute(
        "\nCREATE TABLE news_research_attempts (\n\tid BIGSERIAL NOT NULL, \n\tpackage_id BIGINT NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tstage VARCHAR(16) NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tinput JSONB NOT NULL, \n\toutput JSONB, \n\trules JSONB NOT NULL, \n\tusage JSONB, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(package_id) REFERENCES news_research_packages (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_news_research_attempts_package_id ON news_research_attempts (package_id)"
    )
    op.execute(
        "\nCREATE TABLE news_research_events (\n\tid BIGSERIAL NOT NULL, \n\ttitle TEXT NOT NULL, \n\ttags JSONB NOT NULL, \n\trevision INTEGER NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tresult JSONB, \n\terror TEXT, \n\tdirty_since TIMESTAMP WITH TIME ZONE, \n\tdue_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_news_research_events_due_at ON news_research_events (due_at)"
    )
    op.execute(
        "CREATE INDEX ix_news_research_events_state ON news_research_events (state)"
    )
    op.execute(
        "\nCREATE TABLE news_research_memberships (\n\tevent_id BIGINT NOT NULL, \n\tpackage_id BIGINT NOT NULL, \n\tevidence TEXT NOT NULL, \n\treason TEXT NOT NULL, \n\tchange_kind VARCHAR(20) NOT NULL, \n\tPRIMARY KEY (event_id, package_id), \n\tFOREIGN KEY(event_id) REFERENCES news_research_events (id), \n\tFOREIGN KEY(package_id) REFERENCES news_research_packages (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE news_research_versions (\n\tid BIGSERIAL NOT NULL, \n\tevent_id BIGINT NOT NULL, \n\trevision INTEGER NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tinput JSONB NOT NULL, \n\toutput JSONB, \n\trules JSONB NOT NULL, \n\tusage JSONB, \n\tevidence JSONB NOT NULL, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(event_id) REFERENCES news_research_events (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_news_research_versions_event_id ON news_research_versions (event_id)"
    )
    op.execute(
        "\nCREATE TABLE news_research_references (\n\tid BIGSERIAL NOT NULL, \n\tversion_id BIGINT NOT NULL, \n\ttarget_version_id BIGINT NOT NULL, \n\trole VARCHAR(20) NOT NULL, \n\treason TEXT NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (version_id, target_version_id), \n\tFOREIGN KEY(version_id) REFERENCES news_research_versions (id), \n\tFOREIGN KEY(target_version_id) REFERENCES news_research_versions (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_news_research_references_version_id ON news_research_references (version_id)"
    )


def downgrade():
    op.drop_table("news_research_references")
    op.drop_table("news_research_versions")
    op.drop_table("news_research_memberships")
    op.drop_table("news_research_events")
    op.drop_table("news_research_attempts")
    op.drop_table("news_research_packages")
    op.drop_table("news_research_settings")
