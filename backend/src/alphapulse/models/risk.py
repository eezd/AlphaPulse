"""Durable news risk analysis, events and dashboard snapshots."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from alphapulse.core.db import Base


class RiskSettings(Base):
    __tablename__ = 'risk_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=5, server_default='5')
    provider_id: Mapped[int | None] = mapped_column(ForeignKey('ai_providers.id'))
    model: Mapped[str] = mapped_column(String(128), default='')
    second_provider_id: Mapped[int | None] = mapped_column(ForeignKey('ai_providers.id'))
    second_model: Mapped[str] = mapped_column(String(128), default='')
    third_provider_id: Mapped[int | None] = mapped_column(ForeignKey('ai_providers.id'))
    third_model: Mapped[str] = mapped_column(String(128), default='')
    reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    second_reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    third_reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    grouping_skill_id: Mapped[int | None] = mapped_column(ForeignKey('skill_nodes.id'))
    analysis_skill_id: Mapped[int | None] = mapped_column(ForeignKey('skill_nodes.id'))
    outlook_skill_id: Mapped[int | None] = mapped_column(ForeignKey('skill_nodes.id'))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RiskEvent(Base):
    __tablename__ = 'risk_events'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64))
    entities: Mapped[list] = mapped_column(JSONB)
    stars: Mapped[int] = mapped_column(Integer)
    peak_stars: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RiskAnalysis(Base):
    __tablename__ = 'risk_analyses'
    __table_args__ = (UniqueConstraint('news_id', 'content_hash'),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey('news_items.id'), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey('risk_events.id'), index=True)
    input: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(128))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskSnapshot(Base):
    __tablename__ = 'risk_snapshots'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict | None] = mapped_column(JSONB)
    quotes: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ArticleAnalysis(Base):
    __tablename__ = 'news_ai_article_analyses'
    __table_args__ = (UniqueConstraint('news_id', 'content_hash'),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey('news_items.id'), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    input: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    rules: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(128))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsSummary(Base):
    __tablename__ = 'news_ai_summaries'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64))
    entities: Mapped[list] = mapped_column(JSONB)
    importance_stars: Mapped[int] = mapped_column(Integer, default=1, index=True)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    content_kind: Mapped[str] = mapped_column(String(32), default='report')
    fact: Mapped[str] = mapped_column(Text, default='')
    replacement_ids: Mapped[list] = mapped_column(JSONB, default=list)
    review_config: Mapped[dict | None] = mapped_column(JSONB)
    stars: Mapped[int] = mapped_column(Integer, default=0)
    peak_stars: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dirty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    published_revision: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default='pending')
    error: Mapped[str | None] = mapped_column(Text)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SummaryArticle(Base):
    __tablename__ = 'news_ai_summary_articles'
    __table_args__ = (UniqueConstraint('summary_id', 'analysis_id'),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey('news_ai_summaries.id'), index=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey('news_ai_article_analyses.id'), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    unit: Mapped[dict | None] = mapped_column(JSONB)


class SummaryVersion(Base):
    __tablename__ = 'news_ai_summary_versions'
    __table_args__ = (UniqueConstraint('summary_id', 'revision', 'fingerprint'),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey('news_ai_summaries.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    input: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    rules: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageEvidence(Base):
    __tablename__ = 'news_package_evidence'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey('news_ai_summaries.id'), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey('news_ai_summary_versions.id'), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsEvent(Base):
    __tablename__ = 'news_ai_events'
    published_revision: Mapped[int] = mapped_column(Integer, default=0)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default='pending')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    review_config: Mapped[dict | None] = mapped_column(JSONB)


class EventMember(Base):
    __tablename__ = 'news_ai_event_members'
    package_id: Mapped[int] = mapped_column(ForeignKey('news_ai_summaries.id'), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('news_ai_events.id'), index=True)
    reason: Mapped[str] = mapped_column(Text)


class EventVersion(Base):
    __tablename__ = 'news_ai_event_versions'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('news_ai_events.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    input: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    rules: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupingVersion(Base):
    __tablename__ = 'news_ai_grouping_versions'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey('news_ai_article_analyses.id'), index=True)
    input: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    rules: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PackageCorrection(Base):
    __tablename__ = 'news_ai_package_corrections'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    input: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReprocessJob(Base):
    __tablename__ = 'news_ai_reprocess_jobs'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    settings: Mapped[dict] = mapped_column(JSONB)
    items: Mapped[list] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventLink(Base):
    __tablename__ = 'news_ai_event_links'
    package_id: Mapped[int] = mapped_column(ForeignKey('news_ai_summaries.id'), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('news_ai_events.id'), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    package_revision: Mapped[int] = mapped_column(Integer)
