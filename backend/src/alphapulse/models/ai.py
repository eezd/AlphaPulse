from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alphapulse.core.db import Base


class AiProvider(Base):
    """模型接入配置；api_key 只以密文存储"""

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # openai：/v1/chat/completions；anthropic：/v1/messages
    protocol: Mapped[str] = mapped_column(String(16))
    base_url: Mapped[str] = mapped_column(String(255))
    api_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    api_key_hint: Mapped[str] = mapped_column(String(8))
    model: Mapped[str] = mapped_column(String(128))
    max_tokens: Mapped[int] = mapped_column(Integer, default=8192)
    # 上下文上限（token），超过时拒绝发送而不是静默截断
    context_window: Mapped[int] = mapped_column(Integer, default=200000)
    # 默认思考强度；为空表示不传，由模型决定。Chat 可按轮覆盖
    reasoning_effort: Mapped[str | None] = mapped_column(String(8))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SkillNode(Base):
    """skills 树：文件夹或 skill；内容在 skill_versions，生效的是 published_version_id 指向的版本"""

    __tablename__ = "skill_nodes"
    __table_args__ = (UniqueConstraint("parent_id", "name", postgresql_nulls_not_distinct=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("skill_nodes.id"))
    # folder / skill
    kind: Mapped[str] = mapped_column(String(8))
    scope: Mapped[str] = mapped_column(String(16), default="shared", server_default="shared")
    name: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    published_version_id: Mapped[int | None] = mapped_column(ForeignKey("skill_versions.id", use_alter=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SkillVersion(Base):
    """只追加不修改；回退即重新发布旧版本"""

    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skill_nodes.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    # user / chat
    source: Mapped[str] = mapped_column(String(8))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContextVersion(Base):
    """Chat 的 CONTEXT.md；最新一行即当前内容"""

    __tablename__ = "context_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    # user / chat / reset
    source: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Reset 时结束；未结束的只有一个，即当前会话
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessage(Base):
    """content 为与协议无关的格式：
    user: {"text"}；assistant: {"text", "tool_calls": [{"id","name","arguments"}]}；tool: {"tool_call_id","name","result"}
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    # user / assistant / tool
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict] = mapped_column(JSONB)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id", ondelete="SET NULL"))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
