from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, LargeBinary, String, func
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
    # 交易分析用的模型配置；为空时用默认配置
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id", ondelete="SET NULL"))
    reasoning_effort: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
