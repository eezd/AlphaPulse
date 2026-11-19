from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from alphapulse.core.config import get_settings


class Base(DeclarativeBase):
    pass


# 服务与各爬虫子进程各有独立池，只常驻一个连接，避免空闲连接累积耗尽共享数据库。
engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, pool_size=1, max_overflow=4)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
