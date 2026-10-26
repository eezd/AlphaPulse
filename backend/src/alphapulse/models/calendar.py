from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alphapulse.core.db import Base


class EconomicRelease(Base):
    __tablename__ = "economic_releases"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(160))
    family: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(String(10))
    stage: Mapped[str | None] = mapped_column(String(16))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # scheduled / withdrawn；已公布由 first_received_at 判断，不把到点当成已公布。
    schedule_status: Mapped[str] = mapped_column(String(16), default="scheduled")
    schedule_url: Mapped[str] = mapped_column(Text)
    release_url: Mapped[str | None] = mapped_column(Text)
    schedule_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    # 按指标代码保存实际值、官方报告内前值和引用；数字以十进制字符串存储。
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)


class EconomicRevision(Base):
    __tablename__ = "economic_revisions"
    __table_args__ = (UniqueConstraint("release_id", "revision_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("economic_releases.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    raw_response_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("raw_responses.id"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EconomicSeriesValue(Base):
    """当前官方序列；没有历史公布时点的版本保证，不覆盖当期公告值。"""
    __tablename__ = "economic_series_values"
    __table_args__ = (UniqueConstraint("source", "indicator", "period", "stage"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    indicator: Mapped[str] = mapped_column(String(48))
    period: Mapped[str] = mapped_column(String(10))
    stage: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_response_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("raw_responses.id"))
