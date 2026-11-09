"""Workflow tuning preserves evidence boundaries and limits expensive work."""
import pytest


def test_keyword_queries_match_independently_and_escape_literals(app_env):
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import dialect
    from alphapulse.models import NewsItem
    from alphapulse.news_research.retrieval import terms_for, matching
    terms = terms_for('小鹏 交付 50%')
    condition, score = matching(NewsItem, terms)
    query = select(NewsItem.id).where(condition).order_by(score.desc()).compile(dialect=dialect())
    assert ' OR ' in str(query)
    assert any('50/%' in str(v) for v in query.params.values())
    assert terms_for('ignored', ['央行']) == ['央行','中国人民银行']
    with pytest.raises(ValueError): terms_for('   ')


def test_plan_time_is_distinct_from_occurrence(app_env):
    from alphapulse.news_research.time_contracts import PackageTimePoint
    point = dict(occurred_at=None,time_precision='unknown',time_text='明天',reported_at=None,
        description='拟发布',verification='partial',evidence_refs=[{'article_id':1,'quote':'明天'}])
    value=PackageTimePoint(**point,planned_at='2026-11-03T00:00:00+08:00',planned_precision='day',period_text='2026年10月')
    assert value.occurred_at is None and value.planned_at.day == 3
    with pytest.raises(ValueError): PackageTimePoint(**point,planned_precision='day')
