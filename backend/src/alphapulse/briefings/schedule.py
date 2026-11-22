"""报表窗口和覆盖率规则；时间口径不依赖模型判断。"""
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')
NEW_YORK = ZoneInfo('America/New_York')
KINDS = ('morning', 'evening', 'intraday', 'daily')
PERIOD_KINDS = KINDS[:3]


def window(kind: str, day: date) -> tuple[datetime, datetime, datetime]:
    start = datetime.combine(day, time(), SHANGHAI)
    if kind == 'morning':
        left, right, due = start, start + timedelta(hours=9), start + timedelta(hours=9, minutes=5)
    elif kind == 'evening':
        left, right, due = start + timedelta(hours=9), start + timedelta(hours=18), start + timedelta(hours=18, minutes=5)
    elif kind == 'intraday':
        left, right, due = start + timedelta(hours=18), start + timedelta(days=1), start + timedelta(days=1, minutes=5)
    elif kind == 'daily':
        left, right, due = start, start + timedelta(days=1), start + timedelta(days=1, hours=8, minutes=5)
    else:
        raise ValueError('未知报告类型')
    return left.astimezone(UTC), right.astimezone(UTC), due.astimezone(UTC)


def backfill_start(configuration: dict, default: date = date.min) -> date:
    """全流程共用起始日；旧配置读取时沿用较早的报告起始日。"""
    if 'backfill_start' in configuration:
        value = configuration['backfill_start']
        return date.fromisoformat(value) if value else default
    values = (configuration.get('period_backfill_start'), configuration.get('daily_backfill_start'))
    return min(date.fromisoformat(value) if value else default for value in values)


def backfill_cutoff(configuration: dict) -> datetime:
    start = backfill_start(configuration)
    if start == date.min:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.combine(start, time(), SHANGHAI).astimezone(UTC)


def includes_report(configuration: dict, kind: str, day: date) -> bool:
    return day >= backfill_start(configuration)


def effective_time(news) -> tuple[datetime, str]:
    precision = (news.extra or {}).get('published_at_precision', (news.extra or {}).get('time_precision'))
    unreliable = (news.extra or {}).get('published_at_unreliable', False)
    if news.published_at is not None and precision not in ('date', 'day', 'month', 'unknown') and not unreliable:
        return news.published_at, 'published_at'
    return news.first_seen_at, 'first_seen_at'


def regular_us_open(day: date) -> str:
    # 仅常规时间换算，不声明当天是否交易日。
    return datetime.combine(day, time(9, 30), NEW_YORK).astimezone(SHANGHAI).isoformat()


def coverage_decision(total: int, missing: int, now: datetime, first_check: datetime | None, *, upstream=False):
    if total < 0 or missing < 0 or missing > total:
        raise ValueError('新闻覆盖计数不合法')
    result = {'total': total, 'missing': missing, 'available': total - missing,
              'missing_rate': missing / total if total else 0}
    if total == 0 or missing * 100 <= total * 5:
        return 'ready', result, None
    if upstream:
        return 'blocked', {**result, 'reason': '疑似上游故障'}, None
    if missing >= 10 and missing * 100 >= total * 20:
        return 'blocked', {**result, 'reason': '大批量新闻分析缺失'}, None
    first = first_check or now
    deadline = first + timedelta(hours=1)
    if now >= deadline:
        return 'blocked', {**result, 'reason': '资料等待超过一小时'}, None
    return 'waiting_coverage', result, min(now + timedelta(minutes=15), deadline)
