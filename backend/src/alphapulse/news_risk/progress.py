"""Best-effort actual execution steps, scoped to background news tasks."""
from contextvars import ContextVar
from datetime import UTC, datetime
import logging

current = ContextVar('news_ai_progress', default=None)
active = {}
logger = logging.getLogger(__name__)


def step(phase, **details):
    record = current.get()
    if record is not None:
        record.update(phase=phase, updated_at=datetime.now(UTC).isoformat(), **details)


async def execute(key, action):
    identity = str(key)
    record = {'key': identity, 'stage': key[0], 'object_id': key[1], 'phase': '准备输入',
              'started_at': datetime.now(UTC).isoformat(), 'updated_at': datetime.now(UTC).isoformat()}
    active[identity] = record
    token = current.set(record)
    try:
        return await action()
    finally:
        current.reset(token)
        active.pop(identity, None)
