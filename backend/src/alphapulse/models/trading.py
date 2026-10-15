from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alphapulse.core.db import Base


class TradingAccount(Base):
    """币安模拟盘账户：单行（id=1）。key 只以密文存储；running 是引擎启停的唯一来源"""

    __tablename__ = "trading_account"
    __table_args__ = (CheckConstraint("id = 1", name="trading_account_single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    api_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    api_key_hint: Mapped[str | None] = mapped_column(String(8))
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    running_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 交易资金：仓位计算按 min(交易资金, 账户权益)，不动用账户的全部余额；未设置时不能开始交易
    capital_usdt: Mapped[Decimal | None] = mapped_column(Numeric)
    # 交易分析用的模型配置；为空时用默认配置
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id", ondelete="SET NULL"))
    reasoning_effort: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TradingSymbol(Base):
    __tablename__ = "trading_symbols"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 待执行的分析请求：多个触发在执行前合并为一次
    analysis_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)


class TradingAnalysis(Base):
    """一次模型分析；action=plan 的行同时是一个交易计划，plan_* 等字段记录其生命周期"""

    __tablename__ = "trading_analyses"
    __table_args__ = (Index("ix_trading_analyses_symbol_started", "symbol", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    reasons: Mapped[list[str]] = mapped_column(JSONB)
    # 停止状态下的分析只记录计划、不下单
    dry_run: Mapped[bool] = mapped_column(Boolean)
    # running / done / failed
    status: Mapped[str] = mapped_column(String(16), default="running")
    # plan / keep / exit
    action: Mapped[str | None] = mapped_column(String(8))
    # submit_trade_plan 的参数原样保存
    decision: Mapped[dict | None] = mapped_column(JSONB)
    # 程序算出的各档数量、风险金额、盈亏比等
    sizing: Mapped[dict | None] = mapped_column(JSONB)
    text: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[list | None] = mapped_column(JSONB)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id", ondelete="SET NULL"))
    model: Mapped[str | None] = mapped_column(String(128))
    reasoning_effort: Mapped[str | None] = mapped_column(String(8))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # proposed（待执行器采纳）/ pending（等待 5m 确认，sizing 有 entry 表示已触发）/ open（持仓中）/ closing（平仓中）
    # / closed / canceled（未入场即结束）/ replaced（被新计划替换）/ stale（采纳前已有持仓或已触发，作废）/ applied（exit 决定已执行）
    plan_status: Mapped[str | None] = mapped_column(String(16))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_fill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(32))
    # 平仓后的已实现盈亏、手续费
    result: Mapped[dict | None] = mapped_column(JSONB)


class TradingOrder(Base):
    """订单流水：先写行再发单，clientOrderId / clientAlgoId 为 ap{id}，超时后按它查询而不是换 id 重发"""

    __tablename__ = "trading_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("trading_analyses.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    # entry / tp / stop / close
    purpose: Mapped[str] = mapped_column(String(8))
    # 入场或止盈的档位序号，从 0 开始
    leg: Mapped[int | None] = mapped_column(Integer)
    # 条件单走 /fapi/v1/algoOrder
    is_algo: Mapped[bool] = mapped_column(Boolean)
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(24))
    price: Mapped[Decimal | None] = mapped_column(Numeric)
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric)
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    reduce_only: Mapped[bool] = mapped_column(Boolean)
    # submitting / new / partially_filled / filled / canceled / expired / rejected / finished / failed
    status: Mapped[str] = mapped_column(String(16), default="submitting")
    # orderId 或 algoId
    exchange_id: Mapped[str | None] = mapped_column(String(32))
    filled_qty: Mapped[Decimal] = mapped_column(Numeric, default=0)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TradingEvent(Base):
    """只追加的时间线"""

    __tablename__ = "trading_events"
    __table_args__ = (Index("ix_trading_events_symbol_id", "symbol", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 账户级事件（启停等）为空
    symbol: Mapped[str | None] = mapped_column(String(32))
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("trading_analyses.id"))
    kind: Mapped[str] = mapped_column(String(32))
    # info / warn / error
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSONB)
