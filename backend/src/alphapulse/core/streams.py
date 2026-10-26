"""长连接持续校验会话；取消原生成器以保留 Chat 的中断保存语义。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import HTTPException
from fastapi.sse import ServerSentEvent

from alphapulse.core.auth import AuthSession, check_session


async def session_events(source: AsyncIterator[ServerSentEvent], session: AuthSession) -> AsyncIterator[ServerSentEvent]:
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(source))
            done, _ = await asyncio.wait([pending], timeout=1)
            try:
                await check_session(session)
            except HTTPException as error:
                yield ServerSentEvent(data={"code": error.status_code, "message": error.detail},
                                      event="auth_error" if error.status_code == 401 else "error")
                return
            if done:
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield item
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        await source.aclose()
