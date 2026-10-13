import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scrapy import Request, signals
from scrapy.crawler import Crawler
from scrapy.http import Response
from scrapy.spidermiddlewares.httperror import HttpError
from scrapy_redis.spiders import RedisSpider
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from twisted.python.failure import Failure
from w3lib.html import remove_tags, replace_entities

from alphapulse.core.db import SessionLocal
from alphapulse.crawler.events import write_logs
from alphapulse.models import CrawlRun, NewsItem, Source

# 日志落库失败时只输出到 stdout，避免被自己的处理器再次捕获
sink_logger = logging.getLogger("alphapulse.logsink")
# 日志里的时间按北京时间显示
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

MAX_PAGES = 200
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 15 * 60
# 每轮用于历史回补的时长上限：回补和实时采集共用一个子进程，不能拖住实时轮次
HISTORY_RUN_SECONDS = 20


def ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def html_to_text(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", html)
    text = replace_entities(remove_tags(text))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


@dataclass
class RunState:
    id: uuid.UUID
    trigger: str
    started_at: datetime
    # 早于该时间的内容不再翻页回溯
    cutoff: datetime
    requests: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_revised: int = 0
    errors: list[str] = field(default_factory=list)
    # 遇到 403/429 后本轮不再发新请求
    blocked: bool = False
    # 历史回补：本轮推进到的最早时间；仅在本轮成功结束时写回 sources
    history_target: datetime | None = None
    history_position: datetime | None = None
    history_done: bool = False
    history_deadline: float = 0

    @property
    def full_scan(self) -> bool:
        # 进程启动后的第一轮一直翻到回溯时间，补上次被中断的回溯；常规轮次遇到整页已知即停
        return self.trigger in ("startup", "restart")


class DbLogHandler(logging.Handler):
    """把本来源的日志和全局 WARNING 以上日志收集起来，由刷新任务批量落库并发布"""

    def __init__(self, spider: "NewsSpider") -> None:
        super().__init__(logging.INFO)
        self.spider = spider

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == sink_logger.name:
            return
        if record.name != self.spider.name and record.levelno < logging.WARNING:
            return
        message = record.getMessage()
        # scrapy-redis 每次取到起始任务都会打这一行，没有信息量
        if message.startswith("start req url"):
            return
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        run_id = self.spider.run.id if self.spider.run else None
        self.spider.log_buffer.append((self.spider.name, run_id, record.levelname, message))


class NewsSpider(RedisSpider):
    """一个来源一个子进程。管理进程往 Redis 推一条触发消息即开始一轮采集，引擎空闲即本轮结束。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run: RunState | None = None
        self.log_buffer: list[tuple[str, uuid.UUID | None, str, str]] = []
        self._tasks: set[asyncio.Task] = set()
        # 上一轮成功后的回补进度 (目标, 断点, 是否结束)。写库是异步的，管理进程推来的下一条触发可能还带着旧断点
        self.history_checkpoint: tuple[datetime, datetime, bool] | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler, *args: Any, **kwargs: Any) -> "NewsSpider":
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._on_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider._on_idle, signal=signals.spider_idle)
        crawler.signals.connect(spider._on_error, signal=signals.spider_error)
        crawler.signals.connect(spider._on_closed, signal=signals.spider_closed)
        return spider

    # ---------- 子类实现 ----------

    def first_requests(self) -> Iterable[Request]:
        raise NotImplementedError

    def history_request(self, before: datetime) -> Request:
        """早于 before 的第一页列表请求，meta 需带 history=True；仅 HISTORY_SOURCES 中的来源实现"""
        raise NotImplementedError

    # ---------- 运行生命周期 ----------

    async def start(self):
        for request in self.next_requests():
            yield request

    def make_request_from_data(self, data: bytes) -> list[Request]:
        tick = json.loads(data)
        if self.run is not None:
            # scrapy-redis 的 idle 处理先于 _on_idle 执行：引擎已空闲说明上一轮其实已结束
            if not self.crawler.engine.spider_is_idle():
                self.logger.warning("上一轮尚未结束，跳过本次触发（%s）", tick["trigger"])
                return []
            self._close_run()
        now = datetime.now(UTC)
        self.run = RunState(uuid.uuid4(), tick["trigger"], now, now - timedelta(hours=tick["backfill_hours"]))
        self._spawn(self._insert_run(self.run))
        requests = list(self.first_requests())
        history = tick.get("history")
        if history:
            target = datetime.fromisoformat(history["target"])
            # 断点为空表示首次回补：从现在开始往前翻，顺带补上中间可能的缺口
            position = datetime.fromisoformat(history["position"]) if history["position"] else now
            checkpoint = self.history_checkpoint
            if checkpoint and checkpoint[0] == target and checkpoint[1] <= position:
                position = checkpoint[1]
                if checkpoint[2]:
                    history = None
        if history:
            self.run.history_target = target
            self.run.history_position = position
            self.run.history_deadline = time.monotonic() + HISTORY_RUN_SECONDS
            requests.append(self.history_request(position))
            self.logger.info("开始采集（触发方式：%s，历史回补自 %s 往前）", tick["trigger"], position.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"))
        else:
            self.logger.info("开始采集（触发方式：%s）", tick["trigger"])
        return requests

    def _on_opened(self) -> None:
        logging.getLogger().addHandler(DbLogHandler(self))
        self._spawn(self._flush_logs_forever())
        self.logger.info("采集进程已启动")

    def _on_idle(self) -> None:
        # scrapy-redis 的 idle 处理先执行，可能刚取到新触发并已开始新一轮；引擎仍有待处理请求时不能结束
        if self.run is None or not self.crawler.engine.spider_is_idle():
            return
        self._close_run()

    def _close_run(self) -> None:
        run, self.run = self.run, None
        if run.history_target is not None and not run.errors:
            self.history_checkpoint = (run.history_target, run.history_position, run.history_done)
        self._spawn(self._finish_run(run))

    def _on_error(self, failure: Failure, response: Response) -> None:
        if self.run:
            self.run.errors.append(f"解析异常 {response.url}：{failure.value!r}")

    async def _on_closed(self) -> None:
        self.logger.info("采集进程退出")
        await self._flush_logs()

    def _spawn(self, coro: Coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _insert_run(self, run: RunState) -> None:
        async with SessionLocal() as session:
            await session.execute(
                pg_insert(CrawlRun).values(
                    id=run.id, source_code=self.name, trigger=run.trigger, started_at=run.started_at, status="running"
                ).on_conflict_do_nothing()
            )
            await session.commit()

    async def _finish_run(self, run: RunState) -> None:
        now = datetime.now(UTC)
        failed = bool(run.errors)
        values = {
            "finished_at": now,
            "status": "failed" if failed else "success",
            "requests": run.requests,
            "items_seen": run.items_seen,
            "items_new": run.items_new,
            "items_revised": run.items_revised,
            "error": "\n".join(run.errors)[:4000] if failed else None,
        }
        async with SessionLocal() as session:
            # 与 _insert_run 并发时谁先执行都正确
            await session.execute(
                pg_insert(CrawlRun)
                .values(id=run.id, source_code=self.name, trigger=run.trigger, started_at=run.started_at, **values)
                .on_conflict_do_update(index_elements=[CrawlRun.id], set_=values)
            )
            source = await session.get(Source, self.name, with_for_update=True)
            # 本轮失败不推进断点，下一轮从原位置重做（写入幂等）；目标期间被修改或取消时丢弃本轮进度
            history_saved = not failed and run.history_target is not None and source.history_target == run.history_target
            if history_saved:
                source.history_position = run.history_position
                source.history_done = run.history_done
            if failed:
                source.consecutive_failures += 1
                delay = min(BACKOFF_BASE_SECONDS * 2 ** (source.consecutive_failures - 1), BACKOFF_MAX_SECONDS)
                source.paused_until = now + timedelta(seconds=delay)
                source.last_error = run.errors[0][:1000]
            else:
                source.consecutive_failures = 0
                source.paused_until = None
                source.last_error = None
                source.last_success_at = now
            await session.commit()

        elapsed = (now - run.started_at).total_seconds()
        summary = f"请求 {run.requests}，条目 {run.items_seen}，新增 {run.items_new}，修订 {run.items_revised}，用时 {elapsed:.1f}s"
        if failed:
            self.logger.error("本轮失败：%s；错误 %d 个，暂停 %ds", summary, len(run.errors), delay)
        else:
            self.logger.info("本轮完成：%s", summary)
        if history_saved:
            position = run.history_position.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
            if not run.history_done:
                self.logger.info("历史回补已到 %s", position)
            elif run.history_position <= run.history_target:
                self.logger.info("历史回补完成，已到 %s", position)
            else:
                self.logger.warning("历史回补结束：来源最早只能翻到 %s", position)

    async def _flush_logs(self) -> None:
        rows, self.log_buffer = self.log_buffer, []
        try:
            await write_logs(rows)
        except Exception:
            sink_logger.exception("日志落库失败，丢弃 %d 条", len(rows))

    async def _flush_logs_forever(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self._flush_logs()

    # ---------- 子类使用的工具 ----------

    def request(self, url: str, callback: Callable, **meta: Any) -> Request:
        self.run.requests += 1
        return Request(
            url,
            callback=callback,
            errback=self.on_request_error,
            dont_filter=True,
            meta={"run_id": self.run.id, **meta},
            headers=getattr(self, "request_headers", None),
        )

    def on_request_error(self, failure: Failure) -> None:
        status = failure.value.response.status if failure.check(HttpError) else None
        if status in (403, 429):
            self.run.blocked = True
        message = f"请求失败 {failure.request.url}：{status or repr(failure.value)}"
        self.run.errors.append(message)
        self.logger.error(message)

    async def known_items(self, external_ids: list[str]) -> dict[str, tuple[str, str]]:
        """已入库条目的 (title, summary)，用于判断是否新增、是否需要重抓详情、是否继续翻页"""
        async with SessionLocal() as session:
            rows = await session.execute(
                select(NewsItem.external_id, NewsItem.title, NewsItem.summary)
                .where(NewsItem.source_code == self.name, NewsItem.external_id.in_(external_ids))
            )
        return {external_id: (title, summary) for external_id, title, summary in rows}

    def should_continue(self, external_ids: list[str], known: dict, oldest: datetime, page: int) -> bool:
        """未越过回溯时间、未被限流，且本页有新条目（或本轮为全量扫描）时才翻下一页"""
        return (
            not self.run.blocked
            and page < MAX_PAGES
            and oldest > self.run.cutoff
            and (self.run.full_scan or any(external_id not in known for external_id in external_ids))
        )

    def history_step(self, oldest: datetime | None) -> bool:
        """处理完一页回补列表后调用：推进本轮断点，返回是否继续翻下一页。oldest 为本页最早时间，空页传 None"""
        run = self.run
        if oldest is None or oldest >= run.history_position:
            # 空页或没有更早的数据（部分接口越过下限会返回最新数据）：来源已经翻到底
            run.history_done = True
            return False
        run.history_position = oldest
        if oldest <= run.history_target:
            run.history_done = True
            return False
        return not run.blocked and time.monotonic() < run.history_deadline

    def item(
        self,
        response: Response,
        *,
        external_id: str,
        url: str,
        title: str,
        summary: str,
        content_html: str,
        published_at: datetime,
        author: str | None = None,
        tags: list[str] | None = None,
        is_paywalled: bool = False,
        source_updated_at: datetime | None = None,
        extra: dict | None = None,
    ) -> dict[str, Any]:
        return {
            "source_code": self.name,
            "external_id": external_id,
            "url": url,
            "title": title,
            "summary": summary,
            "content_html": content_html,
            "content_text": html_to_text(content_html),
            "author": author or None,
            "tags": tags or [],
            "is_paywalled": is_paywalled,
            "published_at": published_at,
            "source_updated_at": source_updated_at,
            "extra": extra or {},
            "raw_response_id": response.meta["raw_response_id"],
        }
