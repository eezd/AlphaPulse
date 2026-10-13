from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, PrimaryKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from alphapulse.core.db import Base

# 落库的周期，顺序即补齐优先级（大周期先补，图表最先可看）；1s 只做实时推送不落库
INTERVALS = ["1M", "1w", "3d", "1d", "12h", "8h", "6h", "4h", "2h", "1h", "30m", "15m", "5m", "3m", "1m"]
MARKETS = ("spot", "um")


class MarketSymbol(Base):
    __tablename__ = "market_symbols"
    __table_args__ = (UniqueConstraint("market", "symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # spot 现货 / um U 本位永续
    market: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(32))
    base: Mapped[str] = mapped_column(String(16))
    quote: Mapped[str] = mapped_column(String(16))
    # 由币安 tickSize / stepSize 推出的小数位，用于图表显示
    price_precision: Mapped[int] = mapped_column(Integer)
    volume_precision: Mapped[int] = mapped_column(Integer)
    # pending / backfilling / ready / error / delisted
    status: Mapped[str] = mapped_column(String(16), default="pending")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)


class Kline(Base):
    """时间字段沿用币安的毫秒时间戳；表按 interval 做 LIST 分区（分区在迁移中创建）"""

    __tablename__ = "klines"
    __table_args__ = (
        PrimaryKeyConstraint("market", "symbol", "interval", "open_time"),
        {"postgresql_partition_by": "LIST (interval)"},
    )

    market: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(4))
    open_time: Mapped[int] = mapped_column(BigInteger)
    open: Mapped[Decimal] = mapped_column(Numeric)
    high: Mapped[Decimal] = mapped_column(Numeric)
    low: Mapped[Decimal] = mapped_column(Numeric)
    close: Mapped[Decimal] = mapped_column(Numeric)
    volume: Mapped[Decimal] = mapped_column(Numeric)
    close_time: Mapped[int] = mapped_column(BigInteger)
    quote_volume: Mapped[Decimal] = mapped_column(Numeric)
    trades: Mapped[int] = mapped_column(BigInteger)
    taker_buy_base: Mapped[Decimal] = mapped_column(Numeric)
    taker_buy_quote: Mapped[Decimal] = mapped_column(Numeric)


class KlineSyncState(Base):
    """REST 同步进度：实时 WS 写入不更新这里，所以从 latest_open_time 往后追平即可补上断线缺口"""

    __tablename__ = "kline_sync_state"
    __table_args__ = (PrimaryKeyConstraint("market", "symbol", "interval"),)

    market: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(4))
    earliest_open_time: Mapped[int | None] = mapped_column(BigInteger)
    latest_open_time: Mapped[int | None] = mapped_column(BigInteger)
    # 首次全历史补齐是否完成
    backfill_done: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
