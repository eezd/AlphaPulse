import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alphapulse.core.db import Base


class Source(Base):
    __tablename__ = "sources"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    list_url: Mapped[str] = mapped_column(String(255))
    # 新闻管理器只负责 news；日历专属来源不启动 Scrapy 子进程。
    collector: Mapped[str] = mapped_column(String(16), default="news", server_default="news")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    backfill_hours: Mapped[int] = mapped_column(Integer, default=72)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 历史回补：目标时间（为空表示不回补）、已回补到的最早时间（断点）、是否已结束（到达目标或来源无更早数据）
    history_target: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_position: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("source_code", "external_id"),
        Index("ix_news_items_published_at", "published_at"),
        Index("ix_news_items_source_published_at", "source_code", "published_at"),
        Index("ix_news_items_title_trgm", "title", postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"}),
        Index("ix_news_items_content_trgm", "content_text", postgresql_using="gin", postgresql_ops={"content_text": "gin_trgm_ops"}),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_code: Mapped[str] = mapped_column(ForeignKey("sources.code"))
    external_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    content_html: Mapped[str] = mapped_column(Text, default="")
    content_text: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(128))
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    is_paywalled: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_hash: Mapped[str] = mapped_column(String(64))
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)


class NewsRevision(Base):
    __tablename__ = "news_revisions"
    __table_args__ = (UniqueConstraint("news_id", "revision_no"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news_items.id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    content_html: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    raw_response_id: Mapped[int | None] = mapped_column(ForeignKey("raw_responses.id"))


class RawBody(Base):
    __tablename__ = "raw_bodies"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    # gzip 压缩后的原始响应体；同内容只存一份
    body: Mapped[bytes] = mapped_column(LargeBinary)


class RawResponse(Base):
    __tablename__ = "raw_responses"
    __table_args__ = (Index("ix_raw_responses_source_fetched_at", "source_code", "fetched_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    source_code: Mapped[str] = mapped_column(ForeignKey("sources.code"))
    url: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(8))
    status: Mapped[int] = mapped_column(Integer)
    headers: Mapped[dict] = mapped_column(JSONB)
    body_sha256: Mapped[str] = mapped_column(ForeignKey("raw_bodies.sha256"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (Index("ix_crawl_runs_source_started_at", "source_code", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_code: Mapped[str] = mapped_column(ForeignKey("sources.code"))
    # schedule / startup / restart
    trigger: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # running / success / failed / interrupted
    status: Mapped[str] = mapped_column(String(16))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_revised: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class CrawlLog(Base):
    __tablename__ = "crawl_logs"
    __table_args__ = (Index("ix_crawl_logs_source_id", "source_code", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    source_code: Mapped[str] = mapped_column(ForeignKey("sources.code"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String(8))
    message: Mapped[str] = mapped_column(Text)
