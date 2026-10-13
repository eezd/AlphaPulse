import json
import uuid

from sqlalchemy import insert

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import CrawlLog

LOG_CHANNEL = f"{KEY_PREFIX}pub:logs"
NEWS_CHANNEL = f"{KEY_PREFIX}pub:news"
CONTROL_CHANNEL = f"{KEY_PREFIX}ctl:crawler"
STATUS_KEY = f"{KEY_PREFIX}crawler:status"


def start_urls_key(source_code: str) -> str:
    return f"{KEY_PREFIX}{source_code}:start_urls"


async def write_logs(rows: list[tuple[str, uuid.UUID | None, str, str]]) -> None:
    """rows: (source_code, run_id, level, message)；落库后再发布，前端拿到的日志都带 id"""
    if not rows:
        return
    async with SessionLocal() as session:
        result = await session.execute(
            insert(CrawlLog).returning(CrawlLog.id, CrawlLog.ts),
            [{"source_code": s, "run_id": r, "level": lv, "message": m} for s, r, lv, m in rows],
        )
        saved = result.all()
        await session.commit()
    async with redis.pipeline(transaction=False) as pipe:
        for (source_code, run_id, level, message), (log_id, ts) in zip(rows, saved, strict=True):
            pipe.publish(LOG_CHANNEL, json.dumps({
                "id": log_id,
                "source_code": source_code,
                "run_id": str(run_id) if run_id else None,
                "ts": ts.isoformat(),
                "level": level,
                "message": message,
            }, ensure_ascii=False))
        await pipe.execute()
