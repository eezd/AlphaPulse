"""Independent browser collector: live dates take priority over resumable daily history."""
import asyncio
import json
import logging
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from alphapulse.calendar.jin10 import KINDS, LOCAL, START, day_url, normalize
from alphapulse.calendar.jin10_auth import Jin10Error
from alphapulse.calendar.jin10_browser import Jin10Browser
from alphapulse.calendar.jin10_store import mark_day, next_history_day, save_entry, withdraw_missing
from alphapulse.calendar.service import save_raw
from alphapulse.core.crypto import decrypt
from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import Source
from alphapulse.models.jin10 import Jin10Connection

logger = logging.getLogger("alphapulse.calendar.jin10")
STATUS_KEY = f"{KEY_PREFIX}calendar:jin10"


class ConnectionChanged(Exception):
    pass


def same_connection(row: Jin10Connection | None, encrypted: bytes, connected_at: datetime) -> bool:
    return bool(row and row.token_encrypted == encrypted and row.connected_at == connected_at)


async def persist_day(day: date, responses: dict, encrypted: bytes, connected_at: datetime, now: datetime) -> dict:
    raw_ids = {}
    async with SessionLocal() as session:
        # Disconnect/reconnect takes this same lock. An obsolete fetch must never commit data or progress.
        row = await session.scalar(select(Jin10Connection).where(Jin10Connection.id == 1).with_for_update())
        if not same_connection(row, encrypted, connected_at):
            raise ConnectionChanged()
        for kind in KINDS:
            raw = json.dumps(responses[kind], ensure_ascii=False, separators=(",", ":")).encode()
            raw_ids[kind] = await save_raw(session, "jin10", day_url(day) + f"#{kind}", 200,
                                    {"content-type": "application/json", "transport": "jin10-official-browser-sdk"}, raw, now)
        await session.commit()
    # Retain failed contract responses as evidence, but never advance coverage for them.
    entries = {kind: normalize(responses[kind], kind, day) for kind in KINDS}
    async with SessionLocal() as session:
        row = await session.scalar(select(Jin10Connection).where(Jin10Connection.id == 1).with_for_update())
        if not same_connection(row, encrypted, connected_at):
            raise ConnectionChanged()
        for kind in KINDS:
            for entry in entries[kind]:
                await save_entry(session, entry, raw_ids[kind], now)
            await withdraw_missing(session, day, kind, entries[kind], raw_ids[kind], now)
        counts = {kind: len(entries[kind]) for kind in KINDS}
        await mark_day(session, day, counts, now)
        await session.commit()
    return counts


class Jin10Worker:
    def __init__(self) -> None:
        self.browser = Jin10Browser()
        self.identity = None
        self.live_due: dict[date, float] = {}
        self.retry_due: dict[date, float] = {}
        self.state = {"state": "disconnected", "error": None, "last_success_at": None}

    async def heartbeat(self) -> None:
        while True:
            await redis.set(STATUS_KEY, json.dumps({**self.state, "at": datetime.now(UTC).isoformat()}), ex=60)
            await asyncio.sleep(5)

    async def step(self) -> None:
        async with SessionLocal() as session:
            row = await session.get(Jin10Connection, 1)
            source = await session.get(Source, "jin10")
            identity = (row.token_encrypted, row.connected_at) if row and row.token_encrypted and source and source.enabled else None
        if identity is None:
            await self.browser.close()
            self.identity = None
            self.live_due.clear()
            self.retry_due.clear()
            self.state.update(state="disconnected", error=None, connected_at=None)
            await asyncio.sleep(5)
            return
        if identity != self.identity:
            self.state = {"state": "connecting", "error": None, "connected_at": identity[1].isoformat(), "last_success_at": None}
            await self.browser.connect(decrypt(identity[0]))
            self.identity = identity
        today = datetime.now(LOCAL).date()
        self.retry_due = {d: deadline for d, deadline in self.retry_due.items() if deadline > time.monotonic()}
        dates = [today + timedelta(days=i) for i in range(-1, 8)]
        self.live_due = {d: self.live_due.get(d, 0) for d in dates if d >= START}
        due = sorted((deadline, d) for d, deadline in self.live_due.items() if deadline <= time.monotonic())
        if due:
            # Refresh today first when a process starts, then adjacent days in deadline order.
            day = today if self.live_due.get(today, float("inf")) == 0 else due[0][1]
            history = False
        else:
            async with SessionLocal() as session:
                day = await next_history_day(session, START, today - timedelta(days=1), excluded=set(self.retry_due))
            history = True
        if day is None:
            self.state.update(state="ready", history_next=None)
            await asyncio.sleep(2)
            return
        self.state.update(state="syncing", current_day=day.isoformat(), history=history, error=None)
        try:
            responses = await self.browser.fetch_day(day)
            counts = await persist_day(day, responses, *identity, datetime.now(UTC))
        except (Jin10Error, ValueError, KeyError) as exc:
            # A bad day must not reset progress or starve the remaining live/history dates.
            self.retry_due[day] = time.monotonic() + 300
            if not history:
                self.live_due[day] = self.retry_due[day]
            self.state.update(state="error", error=str(exc))
            logger.warning("金十日历同步失败（%s）：%s", day, exc)
            if isinstance(exc, Jin10Error):
                self.identity = None
                await self.browser.close()
            await asyncio.sleep(5)
            return
        self.retry_due.pop(day, None)
        if not history:
            self.live_due[day] = time.monotonic() + (10 if day == today else 300)
        self.state.update(state="ready", last_success_at=datetime.now(UTC).isoformat(), counts=counts)
        if history:
            self.state["history_next"] = (day + timedelta(days=1)).isoformat()
        # A single collector owns all retries and limits upstream traffic, including backfill.
        await asyncio.sleep(5)

    async def collect(self) -> None:
        while True:
            try:
                await self.step()
            except ConnectionChanged:
                self.identity = None
                self.state.update(state="disconnected", error=None)
            except (Jin10Error, ValueError, KeyError) as exc:
                self.state.update(state="error", error=str(exc))
                logger.warning("金十日历同步失败：%s", exc)
                self.identity = None
                await self.browser.close()
                await asyncio.sleep(30)

    async def run(self) -> None:
        try:
            await asyncio.gather(self.heartbeat(), self.collect())
        finally:
            await self.browser.close()
            await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Jin10Worker().run())
