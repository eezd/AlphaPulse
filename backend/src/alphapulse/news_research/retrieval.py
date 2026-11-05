"""Bounded keyword matching for original news research."""
from sqlalchemy import or_, case

ALIASES = {'央行': '中国人民银行', '证监会': '中国证监会', '人行': '中国人民银行'}


def terms_for(query, keywords=()):
    terms = list(dict.fromkeys(t.strip().casefold() for t in (keywords or query.split()) if t.strip()))[:6]
    if not terms:
        raise ValueError('检索关键词不能为空')
    return list(dict.fromkeys([*terms, *(ALIASES[t] for t in terms if t in ALIASES)]))


def matching(model, terms):
    fields = [model.title, model.fact] if hasattr(model, 'fact') else [model.title, model.content_text]
    matches = [or_(*(field.icontains(term, autoescape=True) for field in fields), model.tags.contains([term])) for term in terms]
    return or_(*matches), sum((case((match, 1), else_=0) for match in matches), 0)
