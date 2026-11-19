"""单篇新闻分析、报表与配置；原始新闻和修订独立保存。"""
from datetime import date, datetime
from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from alphapulse.core.db import Base


class BriefingSettings(Base):
    __tablename__ = 'briefing_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False, server_default='false')
    control_revision: Mapped[int] = mapped_column(default=0, server_default='0')
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default='{}')
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_cursor: Mapped[date | None] = mapped_column(Date)
    history_start: Mapped[date | None] = mapped_column(Date)
    history_end: Mapped[date | None] = mapped_column(Date)
    history_enrolled: Mapped[bool] = mapped_column(default=False, server_default='false')
    telegram_token: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(100))
    telegram_enabled: Mapped[bool] = mapped_column(default=False, server_default='false')


class Analysis(Base):
    __tablename__ = 'briefing_analyses'
    __table_args__ = (
        UniqueConstraint('news_id', 'version_no', name='uq_briefing_analysis_version'),
        Index('uq_briefing_analysis_active', 'news_id', unique=True,
              postgresql_where=text("state IN ('pending', 'processing')")),
        Index('ix_briefing_analysis_queue', 'state', 'lane', 'id'),
        Index('ix_briefing_analysis_keywords', 'keywords', postgresql_using='gin'),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey('news_items.id'), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default='pending')
    lane: Mapped[str] = mapped_column(String(24), default='live')
    control_revision: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    source_revision: Mapped[int | None] = mapped_column(Integer)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    time_basis: Mapped[str] = mapped_column(String(24))
    input: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    rules: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    output: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    keywords: Mapped[list] = mapped_column(JSONB, default=list, server_default='[]')
    importance_score: Mapped[int | None] = mapped_column(Integer)
    importance_stars: Mapped[int | None] = mapped_column(Integer)
    usage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisPart(Base):
    __tablename__ = 'briefing_analysis_parts'
    __table_args__ = (
        UniqueConstraint('analysis_id', 'stage', 'part_index', name='uq_briefing_analysis_part'),
        Index('ix_briefing_analysis_part_fingerprint', 'input_hash', 'state'),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey('briefing_analyses.id', ondelete='RESTRICT'), index=True)
    stage: Mapped[str] = mapped_column(String(16))
    part_index: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default='pending')
    control_revision: Mapped[int] = mapped_column(Integer)
    input: Mapped[dict] = mapped_column(JSONB)
    rules: Mapped[dict] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    usage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    evidence_corrections: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    reused_from: Mapped[int | None] = mapped_column(ForeignKey('briefing_analysis_parts.id', ondelete='RESTRICT'))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(Base):
    __tablename__ = 'briefing_reports'
    __table_args__ = (
        UniqueConstraint('kind', 'report_date', 'version_no', name='uq_briefing_report_version'),
        UniqueConstraint('request_id', name='uq_briefing_report_request'),
        Index('uq_briefing_report_active', 'kind', 'report_date', unique=True,
              postgresql_where=text("state IN ('pending', 'waiting_backfill', 'waiting_coverage', 'processing')")),
        Index('ix_briefing_report_queue', 'state', 'next_check_at', 'lane'),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    report_date: Mapped[date] = mapped_column(Date)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    request_id: Mapped[str | None] = mapped_column(String(36))
    regenerate: Mapped[bool] = mapped_column(default=False, server_default='false')
    lane: Mapped[str] = mapped_column(String(24), default='live')
    state: Mapped[str] = mapped_column(String(24), default='pending')
    control_revision: Mapped[int | None] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    coverage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    market: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    rules: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    output: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportPart(Base):
    __tablename__ = 'briefing_report_parts'
    __table_args__ = (
        UniqueConstraint('report_id', 'part_key', name='uq_briefing_report_part'),
        Index('ix_briefing_part_fingerprint', 'input_hash', 'state'),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('briefing_reports.id'), index=True)
    part_key: Mapped[str] = mapped_column(String(80))
    input_hash: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24), default='pending')
    control_revision: Mapped[int] = mapped_column(Integer)
    input: Mapped[dict] = mapped_column(JSONB)
    rules: Mapped[dict] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    usage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    reused_from: Mapped[int | None] = mapped_column(ForeignKey('briefing_report_parts.id'))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = 'briefing_notifications'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident: Mapped[str] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(String(160), unique=True)
    state: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    message: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


TABLES = [BriefingSettings.__table__, Analysis.__table__, AnalysisPart.__table__, Report.__table__, ReportPart.__table__, Notification.__table__]
