"""固定词表的保守消歧、证据内覆盖与查询扩展；不联网、不改历史结果。"""
import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from alphapulse.briefings.contracts import Keyword, NewsAnalysisResult, in_source, validate_evidence

DATA = Path(__file__).parent / 'data/lexicon/lexicon.json'
PROCESSING_VERSION = 'keywords:v3'
MAX_HINTS = 80
# 普通词/地名的歧义不能靠词表消除；仍允许模型显式选择完整规范名。
AMBIGUOUS_NAMES = {'apple', 'amazon', 'meta', 'turkey', 'georgia', 'congo', 'ether', 'chase', 'fed', 'sec'}


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


class Lexicon:
    def __init__(self, data, digest):
        self.version, self.digest = data['version'], digest
        self.entries = {entry['id']: entry for entry in data['entries']}
        if len(self.entries) != len(data['entries']):
            raise ValueError('词表ID重复')
        names = {}
        for entry in self.entries.values():
            for name in [entry['canonical'], *entry['aliases']]:
                names.setdefault(key(name), set()).add(entry['id'])
        self.names = {name: next(iter(ids)) for name, ids in names.items()
                      if len(ids) == 1 and name not in AMBIGUOUS_NAMES}
        # 按起始词/汉字索引，只编译原文实际遇到的分组，避免每个字符遍历数千公司名。
        self.groups, self.patterns = {}, {}
        self.starts = re.compile(r'[a-zA-Z0-9_]+|[^\x00-\x7f]')
        for name in sorted(self.names, key=lambda value: (-len(value), value)):
            # “银/金”等单字不能在中文复合词中自动识别；完整名称查询仍可使用。
            if len(name) == 1:
                continue
            first = self.starts.match(name)
            if first:
                self.groups.setdefault(first.group(), []).append(name)

    def resolve(self, value):
        identity = self.names.get(key(value))
        return self.entries[identity] if identity else None

    def matches(self, text):
        consumed = 0
        for start in self.starts.finditer(text):
            prefix = start.group().casefold()
            if start.start() < consumed or prefix not in self.groups:
                continue
            if prefix not in self.patterns:
                self.patterns[prefix] = re.compile('|'.join(
                    re.escape(name).replace(r'\ ', r'\s+')
                    + (r'(?![a-zA-Z0-9_])' if name[-1].isascii() and name[-1].isalnum() else '')
                    for name in self.groups[prefix]), re.IGNORECASE)
            match = self.patterns[prefix].match(text, start.start())
            if match and (entry := self.resolve(match.group())):
                consumed = match.end()
                if entry['id'] == 'cldr:currencies:EUR' and re.match(
                        r'(?:\s*[- ]?\s*(?:area|zone)(?![a-zA-Z0-9_])|区)', text[match.end():], re.IGNORECASE):
                    continue
                yield entry, match.group(), match.start(), match.end()

    def metadata(self):
        return {'version': self.version, 'sha256': self.digest, 'processing_version': PROCESSING_VERSION}


@lru_cache(maxsize=1)
def lexicon():
    raw = DATA.read_bytes()
    return Lexicon(json.loads(raw), hashlib.sha256(raw).hexdigest())


def candidates(article):
    """只提示真实命中的名称；有界提示不裁剪原文，也不宣称穷尽全部实体。"""
    dictionary = lexicon()
    found = {}
    for field in ('title', 'summary', 'content_text'):
        for entry, alias, _, _ in dictionary.matches(article.get(field) or ''):
            if entry['id'] not in found:
                found[entry['id']] = {'canonical': entry['canonical'], 'kind': entry['kind'],
                                      'alias': alias, 'field': field, 'quote': alias}
    return {'items': list(found.values())[:MAX_HINTS], 'matched_count': len(found),
            'truncated': len(found) > MAX_HINTS}


def query_names(query):
    entry = lexicon().resolve(query)
    if entry is None:
        return None
    return sorted({name for name in [entry['canonical'], *entry['aliases']]
                   if lexicon().resolve(name) == entry}, key=lambda name: (len(name), name))


def normalize(result, article):
    """剔除无原文证据的别名并审计；事实与引文仍须逐字有效。"""
    raw = result.model_dump()
    original = raw['keywords']
    decisions, retained = [], []
    for word in original:
        aliases = []
        for alias in word['aliases']:
            if in_source(alias, article):
                aliases.append(alias)
            else:
                decisions.append({'action': 'drop_invalid_alias', 'canonical': word['canonical'],
                                  'alias': alias, 'reason': 'not_literal_in_single_field'})
        if not aliases:
            raise ValueError('关键词无可保留的原文别名')
        retained.append({**word, 'aliases': aliases})
    result = NewsAnalysisResult.model_validate({**raw, 'keywords': retained})
    validate_evidence(result, article)
    dictionary = lexicon()
    words = []
    for word in result.keywords:
        canonical = dictionary.resolve(word.canonical)
        resolved = [dictionary.resolve(alias) for alias in word.aliases]
        identities = {entry['id'] for entry in resolved if entry}
        # 只有规范名与原文别名共同指向同一条目才统一；不能把“比特币ETF”
        # 仅因别名出现BTC缩成比特币，也不能把公司股票缩成公司主体。
        selected = canonical if canonical and identities == {canonical['id']} else None
        # 公司名和具体证券不能仅凭别名互换；国别也不能消歧同名人物/机构。
        if selected and ((selected['kind'] == 'entity' and word.kind == 'asset')
                         or (selected['kind'] == 'country' and word.kind != 'country')):
            selected = None
        updated = word.model_copy(update={'canonical': selected['canonical'], 'kind': selected['kind']}) if selected else word
        if updated != word:
            decisions.append({'action': 'normalize', 'before': word.model_dump(), 'after': updated.model_dump(), 'id': selected['id']})
        elif identities and (not selected):
            decisions.append({'action': 'ambiguous', 'canonical': word.canonical})
        existing = next((item for item in words if key(item.canonical) == key(updated.canonical)), None)
        if existing:
            if existing.kind != updated.kind:
                # 同一规范名但证券/主体等角色冲突时，保留过滤后的结果，不强行合并。
                return result, {**dictionary.metadata(), 'raw_keywords': original,
                                'decisions': decisions, 'conflict': updated.canonical, 'added': []}
            existing.aliases = list(dict.fromkeys([*existing.aliases, *updated.aliases]))
        else:
            words.append(updated.model_copy(deep=True))
    # 不扫描全文补标签：只有模型已选择的事实证据才可自动补回已知对象。
    # 人名/陌生主体及全文未被选中的技术信息留给候选提示与语义复核。
    added = []
    present = {key(word.canonical) for word in words}
    selected_aliases = {entry['id'] for word in words for alias in word.aliases
                        if (entry := dictionary.resolve(alias))}
    # 原文别名已表达完整对象时，不能再从该次出现内部拆出另一资产。
    # 使用位置范围而非全局禁用名称，保留同一引文中独立出现的基础资产。
    alias_patterns = [re.compile(re.escape(alias), re.IGNORECASE)
                      for alias in {alias for word in result.keywords for alias in word.aliases}]
    for fact in result.facts:
        protected = [match.span() for pattern in alias_patterns for match in pattern.finditer(fact.quote)]
        for entry, alias, start, end in dictionary.matches(fact.quote):
            if any(left <= start and end <= right and (left, right) != (start, end)
                   for left, right in protected):
                continue
            if entry['kind'] not in ('asset', 'topic'):
                continue
            if key(entry['canonical']) in present or entry['id'] in selected_aliases:
                continue
            words.append(Keyword(canonical=entry['canonical'], aliases=[alias], kind=entry['kind'], quote=fact.quote))
            added.append({'id': entry['id'], 'alias': alias, 'basis': 'selected_fact_quote'})
            present.add(key(entry['canonical']))
            selected_aliases.add(entry['id'])
    output = NewsAnalysisResult.model_validate({**result.model_dump(), 'keywords': [word.model_dump() for word in words]})
    validate_evidence(output, article)
    hints = article.get('keyword_candidates') or candidates(article)
    uncovered = [item for item in hints['items'] if key(item['canonical']) not in present]
    return output, {**dictionary.metadata(), 'raw_keywords': original, 'decisions': decisions, 'added': added,
                    'unselected_candidates': uncovered, 'candidate_count': hints['matched_count'],
                    'candidates_truncated': hints['truncated']}
