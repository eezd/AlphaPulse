"""Additive storage: original news and legacy AI tables are never written here."""

from datetime import datetime
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    text,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from alphapulse.core.db import Base


class ResearchSettings(Base):
    __tablename__ = "news_research_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(default=False)
    control_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    realtime_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Package(Base):
    __tablename__ = "news_research_packages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news_items.id"), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="index_pending", index=True)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Attempt(Base):
    __tablename__ = "news_research_attempts"
    __table_args__ = (Index("uq_news_attempt_active", "package_id", unique=True, postgresql_where=text("state = 'processing'")),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    package_id: Mapped[int] = mapped_column(
        ForeignKey("news_research_packages.id"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    input: Mapped[dict] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    rules: Mapped[dict] = mapped_column(JSONB)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Event(Base):
    __tablename__ = "news_research_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    realtime_pending: Mapped[bool] = mapped_column(default=False, server_default="false")
    dirty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Membership(Base):
    __tablename__ = "news_research_memberships"
    event_id: Mapped[int] = mapped_column(
        ForeignKey("news_research_events.id"), primary_key=True
    )
    package_id: Mapped[int] = mapped_column(
        ForeignKey("news_research_packages.id"), primary_key=True
    )
    evidence: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    change_kind: Mapped[str] = mapped_column(String(20))


class Version(Base):
    __tablename__ = "news_research_versions"
    __table_args__ = (Index("uq_news_version_active", "event_id", unique=True, postgresql_where=text("state = 'processing'")),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("news_research_events.id"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    input: Mapped[dict] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    rules: Mapped[dict] = mapped_column(JSONB)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Reference(Base):
    __tablename__ = "news_research_references"
    __table_args__ = (UniqueConstraint("version_id", "target_version_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("news_research_versions.id"), index=True
    )
    target_version_id: Mapped[int] = mapped_column(
        ForeignKey("news_research_versions.id")
    )
    role: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)


TABLES = [
    ResearchSettings.__table__,
    Package.__table__,
    Attempt.__table__,
    Event.__table__,
    Membership.__table__,
    Version.__table__,
    Reference.__table__,
]


class Batch(Base):
    __tablename__ = 'news_research_batches'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phase: Mapped[str] = mapped_column(String(20), default='assigning', index=True)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchPackage(Base):
    __tablename__ = 'news_research_batch_packages'
    batch_id: Mapped[int] = mapped_column(ForeignKey('news_research_batches.id'), primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey('news_research_packages.id'), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default='pending')


class BatchEvent(Base):
    __tablename__ = 'news_research_batch_events'
    batch_id: Mapped[int] = mapped_column(ForeignKey('news_research_batches.id'), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('news_research_events.id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default='pending')


TABLES.extend([Batch.__table__, BatchPackage.__table__, BatchEvent.__table__])
