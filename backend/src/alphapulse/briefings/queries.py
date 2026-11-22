"""关键词检索和用量展示共用的轻量投影。"""
import re
from sqlalchemy import Text, cast, exists, func, or_, select, text
from alphapulse.briefings.keywords import key, query_names


def keyword_name_key(value):
    # PostgreSQL 18 的 Unicode casefold 与 Python key()使用相同名称口径。
    normalized = func.casefold(func.normalize(value, text('NFKC')).collate('pg_unicode_fast'))
    return func.btrim(func.regexp_replace(normalized, '[\\s\x1c-\x1f\x85]+', ' ', 'g'))


def keyword_match(column, query):
    names = query_names(query)
    if names:
        # 对已消歧的完整名称作元素相等匹配，母机构查询不吞并独立研究部。
        values = func.jsonb_array_elements_text(func.jsonb_path_query_array(
            column, text("'$[*].canonical'::jsonpath"))).table_valued('value').alias('keyword_names')
        aliases = func.jsonb_array_elements_text(func.jsonb_path_query_array(
            column, text("'$[*].aliases[*]'::jsonpath"))).table_valued('value').alias('keyword_aliases')
        normalized_names = sorted({key(name) for name in names})
        return or_(exists(select(1).select_from(values).where(keyword_name_key(values.c.value).in_(normalized_names))),
                   exists(select(1).select_from(aliases).where(keyword_name_key(aliases.c.value).in_(normalized_names))))
    canonical = cast(func.jsonb_path_query_array(column, text("'$[*].canonical'::jsonpath")), Text)
    aliases = cast(func.jsonb_path_query_array(column, text("'$[*].aliases[*]'::jsonpath")), Text)
    if query.casefold() in ('btc', 'eth', 'ether'):
        pattern = rf'(?i)(^|[^a-z0-9_]){re.escape(query)}([^a-z0-9_]|$)'
        return or_(canonical.regexp_match(pattern), aliases.regexp_match(pattern))
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return or_(canonical.ilike(f'%{escaped}%', escape='\\'), aliases.ilike(f'%{escaped}%', escape='\\'))


def usage_totals(records):
    """只累计每个独立调用的 usage；复用步骤不重复计费，缺失明确标为未知。"""
    result = {'input_tokens': 0, 'output_tokens': 0, 'unknown_calls': 0, 'known_calls': 0, 'reused_steps': 0}
    def add(value):
        if isinstance(value, dict) and ('input_tokens' in value or 'output_tokens' in value):
            result['known_calls'] += 1
            result['input_tokens'] += value.get('input_tokens', 0)
            result['output_tokens'] += value.get('output_tokens', 0)
        elif isinstance(value, dict) and isinstance(value.get('rounds'), list):
            for item in value['rounds']:
                add(item)
        else:
            result['unknown_calls'] += 1
    for record in records:
        if record.reused_from is not None:
            result['reused_steps'] += 1
        elif record.started_at is not None:
            add(record.usage)
    return result
