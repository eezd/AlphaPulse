"""新闻信号的统计（纯函数，便于测试）：按模型标注方向交易的收益，同一件事的多条报道只算一次"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

# 多个来源报道同一件事：同一类别 10 分钟内只算第一条
DEDUPE = timedelta(minutes=10)
# 吃单进出各 0.04%
FEE_BP = 8.0
DIRECTION_SIGN = {"bullish": 1, "bearish": -1}


@dataclass
class Signal:
    published_at: datetime
    category: str
    direction: str
    # {symbol: {"entry": ..., "1h": ..., "4h": ...}}，价格为十进制字符串
    prices: dict


def dedupe(signals: list[Signal]) -> list[Signal]:
    """signals 按发布时间排序"""
    last: dict[str, datetime] = {}
    kept = []
    for s in signals:
        if s.category not in last or s.published_at - last[s.category] > DEDUPE:
            kept.append(s)
            last[s.category] = s.published_at
    return kept


def summarize(signals: list[Signal], symbols: tuple[str, ...], horizons: tuple[str, ...]) -> list[dict]:
    """每个类别、币、持有时长：利多做多、利空做空的收益（bp），中性不交易"""
    rows = []
    kept = dedupe(signals)
    for category in sorted({s.category for s in kept}):
        for symbol in symbols:
            for horizon in horizons:
                pnl = [
                    DIRECTION_SIGN[s.direction] * (float(s.prices[symbol][horizon]) / float(s.prices[symbol]["entry"]) - 1) * 1e4
                    for s in kept if s.category == category and s.direction in DIRECTION_SIGN
                ]
                n = len(pnl)
                mean = sum(pnl) / n if n else None
                sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / (n - 1)) if n > 1 else None
                rows.append({
                    "category": category, "symbol": symbol, "horizon": horizon, "n": n,
                    "mean_bp": mean, "t": mean / (sd / math.sqrt(n)) if sd else None,
                    "net_bp": mean - FEE_BP if mean is not None else None,
                    "win_rate": sum(x > 0 for x in pnl) / n if n else None,
                })
    return rows
