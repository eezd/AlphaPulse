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
    provider_id: Mapped[int | None] = mapped_column(ForeignKey('ai_providers.id'))
    model: Mapped[str] = mapped_column(String(128), default='')


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
