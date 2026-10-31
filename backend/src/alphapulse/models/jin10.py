"""Encrypted connection and short-lived, session-owned QR login state."""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alphapulse.core.db import Base


class Jin10Connection(Base):
    __tablename__ = "jin10_connection"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_id: Mapped[str | None] = mapped_column(String(64))
    owner: Mapped[str | None] = mapped_column(String(64))
    qr_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Jin10SyncDay(Base):
    """A day is covered only after all three authenticated responses were validated."""
    __tablename__ = "jin10_sync_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict] = mapped_column(JSONB)
