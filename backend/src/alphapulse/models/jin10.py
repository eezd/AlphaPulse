"""Encrypted connection and short-lived, session-owned QR login state."""
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, Text
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
