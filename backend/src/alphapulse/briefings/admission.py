"""机构资料的分析、报告与展示准入；历史观点不因补采进入当日报告。"""
from datetime import date
from sqlalchemy import Date, DateTime, and_, case, cast, exists, func, or_
from alphapulse.briefings.schedule import SHANGHAI
from alphapulse.core.research_sources import SOURCES
from alphapulse.models.news import NewsItem, Source
from alphapulse.briefings.models import Analysis

RESEARCH_CODES = ('citi_insights', 'jpmorgan_research', 'goldman_insights')
RESEARCH_MARKER = '<!-- briefing:research:v1 -->'


def publication_date(extra):
    value = extra.get('published_date')
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def research_context(news, source):
    published = publication_date(news.extra)
    seen = news.first_seen_at.astimezone(SHANGHAI).date()
    activated = source.research_enabled_at
    if activated is None or news.first_seen_at < activated:
        reason = 'historical'
    elif published is None:
        reason = 'unknown_date'
    elif published > seen:
        reason = 'future_date'
    elif published < seen:
        reason = 'late_arrival'
    else:
        reason = 'new_publication'
    return {'mode': 'report' if reason == 'new_publication' else 'archive', 'reason': reason,
            'activated_at': activated.isoformat() if activated else None,
            'published_date': published.isoformat() if published else None}


def view(news, source):
    if source is None or source.analysis_policy != 'automatic':
        return {'mode': 'collect_only', 'reason': '来源尚未启用分析'}
    if news.source_code not in SOURCES:
        return {'mode': 'report', 'reason': '普通新闻按既有时间窗口参与分析与报告'}
    if news.source_code not in RESEARCH_CODES or source.research_enabled_at is None:
        return {'mode': 'collect_only', 'reason': '机构来源尚未接入研究分析'}
    if (news.extra.get('research', {}).get('content_status') != 'full'
            or not (news.content_text or '').strip()):
        return {'mode': 'collect_only', 'reason': '未取得可分析的网页全文文字'}
    context = research_context(news, source)
    reasons = {'historical': '首次接入前资料，仅建库', 'unknown_date': '发表日期未知或无效，仅建库',
               'future_date': '发表日期晚于采集日，仅建库', 'late_arrival': '跨日补采的研究，仅建库',
               'new_publication': '接入后首次采集的同日新文章'}
    return {'mode': context['mode'], 'reason': reasons[context['reason']]}


def source_allowed():
    return exists().where(Source.code == NewsItem.source_code, Source.analysis_policy == 'automatic',
        or_(~NewsItem.source_code.in_(SOURCES), and_(NewsItem.source_code.in_(RESEARCH_CODES),
                                                    Source.research_enabled_at.is_not(None)))).correlate(NewsItem)


def analysis_allowed(news_id=None):
    allowed = and_(source_allowed(), or_(~NewsItem.source_code.in_(SOURCES), and_(
        NewsItem.extra['research']['content_status'].as_string() == 'full',
        NewsItem.content_text.regexp_match(r'\S'))))
    return allowed if news_id is None else exists().where(NewsItem.id == news_id, allowed)


def publication_expression():
    value = NewsItem.extra['published_date'].as_string()
    # JSON 属于输入边界；无效日期不触发整批 SQL 转换失败，也不获得当日报告资格。
    return case((and_(value.regexp_match(r'^\d{4}-\d{2}-\d{2}$'),
                       func.pg_input_is_valid(value, 'date')), cast(value, Date)), else_=None)


def archive_time(ordinary):
    published = publication_expression()
    return case((NewsItem.source_code.in_(RESEARCH_CODES),
                 func.coalesce(func.timezone('Asia/Shanghai', cast(published, DateTime)),
                               NewsItem.first_seen_at)), else_=ordinary)


def report_news_allowed():
    activated = exists().where(Source.code == NewsItem.source_code,
                               NewsItem.first_seen_at >= Source.research_enabled_at).correlate(NewsItem)
    return and_(analysis_allowed(), or_(~NewsItem.source_code.in_(SOURCES), and_(activated,
        publication_expression() == cast(func.timezone('Asia/Shanghai', NewsItem.first_seen_at), Date))))


def report_analysis_allowed():
    # 使用成功版本的冻结资格，不让当前正文或发表日期修订移动历史分析。
    source = exists().where(NewsItem.id == Analysis.news_id, source_allowed()).correlate(Analysis)
    institutional = exists().where(NewsItem.id == Analysis.news_id, NewsItem.source_code.in_(SOURCES)).correlate(Analysis)
    return and_(source, func.coalesce(or_(~institutional, and_(
        Analysis.input['research_context']['mode'].as_string() == 'report',
        Analysis.input['research_context']['reason'].as_string() == 'new_publication')), False))
