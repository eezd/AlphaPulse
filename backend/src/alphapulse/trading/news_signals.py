"""新闻信号前向记录：给新进来的加密相关新闻打标签（类别、对 BTC 的方向），发布 4 小时后回填 BTC/ETH 的价格。

研究（2026-10-16，见 CONTEXT.md）：事件新闻按模型标注方向持有 4 小时，样本内约 +15bp 但不显著，且模型可能记得那段行情。
前向记录只看标注时还没发生的行情，用来确认或否定这条线索。只记录，不下单。
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from alphapulse.ai import llm
from alphapulse.ai.tools import LOCAL_TZ
from alphapulse.core.db import SessionLocal
from alphapulse.models import AiProvider, Kline, NewsItem, NewsSignal, TradingAccount

logger = logging.getLogger("alphapulse.trading.news_signals")

# 研究样本截止到 2026-10-16 14:15，之后发布的新闻才计入，两份样本互不重叠
FORWARD_START = datetime(2026, 10, 16, 15, 0, tzinfo=LOCAL_TZ)
CRYPTO_PATTERN = "比特币|加密货币|以太坊|加密市场|数字货币|稳定币|Coinbase|微策略|币安|BTC"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
# 发布所在分钟之后第几分钟的 1m 开盘价：发布后第 5 分钟进场（抓取加分析约 1–3 分钟），持有 1h、4h
OFFSETS = {"open": 0, "entry": 5, "1h": 65, "4h": 245}
LOOP_SECONDS = 60
BATCH = 40
CATEGORIES = {"price_report", "event", "irrelevant"}
DIRECTIONS = {"bullish", "bearish", "neutral"}

SYSTEM = """你是加密货币新闻分类员。只根据给出的新闻文字判断，不要使用你对之后行情的任何记忆。
对每条新闻给出：
- category：price_report（只是在报道价格已经发生的涨跌，如「比特币跌破 78000 美元」「比特币日内涨超 4%」，包括概念股跟随币价的涨跌）；
  event（会影响币价的事件或信息，如机构增持/减持、ETF 资金流、监管政策、宏观政策对加密市场的影响、黑客攻击、交易所动态）；
  irrelevant（与 BTC/ETH 价格基本无关，如个别公司财报细节、分析师调整目标价、技术合作）。
- direction：对 BTC 价格短期（几小时内）的影响，bullish / bearish / neutral。price_report 按它报道的涨跌方向填。
用 label 工具一次提交全部结果，id 与输入一一对应。"""
TOOL = llm.ToolSpec("label", "提交分类结果", {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "category": {"type": "string", "enum": sorted(CATEGORIES)},
            "direction": {"type": "string", "enum": sorted(DIRECTIONS)},
        },
        "required": ["id", "category", "direction"],
    }}},
    "required": ["items"],
})


def news_text():
    """快讯常常没有标题，取正文开头"""
    return func.coalesce(func.nullif(NewsItem.title, ""), func.left(NewsItem.content_text, 120))


def clean(text: str) -> str:
    # 去掉「财联社10月7日电，」之类的日期前缀，减少模型联想到具体日期
    return re.sub(r"^(财联社)?\d{1,2}月\d{1,2}日电[，,]?", "", text).strip()[:200]


async def label_new() -> int:
    """给还没打标签的加密新闻打标签，返回本轮标注条数。模型漏掉的条目下一轮重试"""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(NewsItem.id, news_text()).outerjoin(NewsSignal, NewsSignal.news_id == NewsItem.id)
            .where(NewsSignal.news_id.is_(None), NewsItem.published_at >= FORWARD_START, news_text().op("~")(CRYPTO_PATTERN))
            .order_by(NewsItem.published_at).limit(BATCH)
        )).all()
        if not rows:
            return 0
        account = await session.get(TradingAccount, 1)
        provider = await session.get(AiProvider, account.provider_id) if account and account.provider_id else await session.scalar(
            select(AiProvider).where(AiProvider.is_default)
        )
        if provider is None:
            raise RuntimeError("没有可用的模型配置，无法给新闻打标签")
        session.expunge(provider)

    completion = None
    text = "\n".join(f"{news_id}\t{clean(t)}" for news_id, t in rows)
    async for item in llm.stream(provider, SYSTEM, [{"role": "user", "text": text}], [TOOL], "low"):
        if isinstance(item, llm.Completion):
            completion = item
    call = next((c for c in completion.tool_calls if c["name"] == "label"), None)
    if call is None:
        raise RuntimeError("模型没有调用 label 工具")
    ids = {news_id for news_id, _ in rows}
    values = [
        {"news_id": x["id"], "category": x["category"], "direction": x["direction"], "model": provider.model}
        for x in call["arguments"].get("items", [])
        if x.get("id") in ids and x.get("category") in CATEGORIES and x.get("direction") in DIRECTIONS
    ]
    if len(values) < len(rows):
        logger.warning("新闻标签：%d 条中模型只返回了 %d 条有效结果，其余下一轮重试", len(rows), len(values))
    if values:
        async with SessionLocal() as session:
            await session.execute(insert(NewsSignal).values(values).on_conflict_do_nothing())
            await session.commit()
    return len(values)


async def fill_prices() -> int:
    """发布满 4 小时的信号回填价格，返回回填条数。1m K 线有缺口时留到下一轮"""
    cutoff = datetime.now(UTC) - timedelta(minutes=OFFSETS["4h"] + 2)
    filled = 0
    async with SessionLocal() as session:
        pending = (await session.execute(
            select(NewsSignal, NewsItem.published_at).join(NewsItem, NewsItem.id == NewsSignal.news_id)
            .where(NewsSignal.prices.is_(None), NewsItem.published_at <= cutoff)
        )).all()
        for signal, published_at in pending:
            minute = int(published_at.timestamp()) // 60 * 60_000
            times = {key: minute + offset * 60_000 for key, offset in OFFSETS.items()}
            prices = {}
            for symbol in SYMBOLS:
                opens = dict((await session.execute(
                    select(Kline.open_time, Kline.open).where(
                        Kline.market == "um", Kline.symbol == symbol, Kline.interval == "1m", Kline.open_time.in_(times.values()),
                    )
                )).tuples().all())
                if len(opens) < len(times):
                    break
                prices[symbol] = {key: str(opens[t]) for key, t in times.items()}
            else:
                signal.prices = prices
                filled += 1
        await session.commit()
    return filled


async def news_signal_loop() -> None:
    while True:
        try:
            labeled = await label_new()
            filled = await fill_prices()
            if labeled or filled:
                logger.info("新闻信号：标注 %d 条，回填价格 %d 条", labeled, filled)
        except Exception:
            logger.exception("新闻信号记录出错，稍后重试")
        await asyncio.sleep(LOOP_SECONDS)
