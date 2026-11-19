"""完整新闻分段：原文范围可核对，map 输出有冻结字段内的逐字证据。"""
import hashlib
import json
import re
from typing import Literal
from pydantic import Field
from alphapulse.ai import submission
from alphapulse.briefings import contracts, keywords

MAX_PARTS = 32
MAX_SOURCE_BYTES = 180_000
MAX_MAP_BYTES = 8_192
MAX_TOTAL_MAP_BYTES = 32 * 4_096
OVERLAP = 1_000


class Evidence(contracts.StrictModel):
    field: Literal['title', 'summary', 'content_text']
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote: str = Field(min_length=1)
    anchor: str | None = Field(default=None, min_length=8, max_length=240)


class MapFact(contracts.StrictModel):
    text: str = Field(min_length=1)
    evidence: Evidence


class MapKeyword(contracts.StrictModel):
    canonical: str = Field(min_length=1)
    kind: Literal['entity', 'asset', 'country', 'market', 'topic', 'action']
    quote: Evidence
    aliases: list[Evidence] = Field(default_factory=list)


class MapResult(contracts.StrictModel):
    summary: str = Field(max_length=600)
    facts: list[MapFact]
    keywords: list[MapKeyword]


def map_json(result):
    return json.dumps(result.model_dump(exclude_none=True), ensure_ascii=False, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def fits_reserved(provider, system, payload, schema):
    source = system + json.dumps(payload, ensure_ascii=False) + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (submission.fits_context(provider, system, payload, schema)
            and len(source.encode('utf-8')) + 4 * provider.max_tokens <= min(provider.context_window, submission.MAX_CONTEXT_WINDOW))


def boundaries(content, max_bytes):
    if not content:
        return []
    parts, start = [], 0
    while start < len(content):
        end, used = start, 0
        while end < len(content):
            width = len(content[end].encode('utf-8'))
            if used + width > max_bytes:
                break
            used += width
            end += 1
        if end <= start:
            raise ValueError('单个原文字符超出分段预算')
        if end < len(content):
            paragraph = content.rfind('\n', max(start + 1, end - 2_000), end)
            if paragraph > start:
                end = paragraph + 1
        parts.append((start, end))
        if len(parts) > MAX_PARTS:
            raise ValueError('完整新闻超过32段预算；未截断或发送')
        if end == len(content):
            break
        start = max(start + 1, end - OVERLAP)
    position = 0
    for left, right in parts:
        if not (left <= position < right):
            raise ValueError('新闻分段未完整覆盖原文')
        position = right
    if position != len(content):
        raise ValueError('新闻分段未完整覆盖原文')
    return parts


def material_anchors(article, start, end):
    """提示官方正文开头明确标注的行动状态和日期，用于质量评审。"""
    content = article['content_text']
    if start >= 20_000:
        return []
    anchors = []
    for header in re.finditer(r'\b(ACTION|DATES):\s*', content[start:min(end, 20_000)], re.I):
        kind = header.group(1).lower()
        position = start + header.end()
        section = content[position:min(end, position + 600)]
        stop = re.search(r'\b(?:ADDRESSES|FOR FURTHER INFORMATION CONTACT|SUPPLEMENTARY INFORMATION):', section, re.I)
        if stop:
            section = section[:stop.start()]
        for sentence in re.finditer(r'[^.!?]+[.!?](?=\s|$)', section, re.S):
            value = sentence.group().strip()
            if not value or len(value) > 240:
                continue
            if kind == 'action' and not re.search(r'\b(?:proposed|final|interim)\s+rule\b', value, re.I):
                continue
            if kind == 'dates' and not re.search(r'\b(?:comments?|effective|deadline|implementation)\b', value, re.I):
                continue
            offset = position + sentence.start() + len(sentence.group()) - len(sentence.group().lstrip())
            if kind == 'dates':
                label = re.match(r'(?i)effective date:\s*', value)
                if label:
                    offset += label.end()
                    value = value[label.end():]
            anchors.append({'kind': kind, 'field': 'content_text', 'start': offset,
                            'end': offset + len(value), 'quote': value})
            if kind == 'action' or len(anchors) >= 3:
                break
        if len(anchors) >= 3:
            break
    return anchors


def condition_leads(article, start, end):
    """只提示冻结正文中的限定词，不能把背景条件当作当前事实。"""
    content = article['content_text']
    if start >= 60_000:
        return []
    patterns = (
        ('eligibility', r'\b(?:for (?:the|this) [^.!?\n]{0,40} to apply|only if|provided that)\b'),
        ('scope', r'\b(?:solely for|limited to|direct and unconditional)\b'),
        ('limitation', r'\b(?:without further action|unless|not registered under)\b'),
    )
    leads = []
    for kind, pattern in patterns:
        found = re.search(pattern, content[start:min(end, 60_000)], re.I)
        if found:
            left, right = start + found.start(), start + found.end()
            leads.append({'kind': kind, 'field': 'content_text', 'start': left, 'end': right,
                          'quote': content[left:right]})
    return sorted(leads, key=lambda lead: lead['start'])


def map_payload(article, start, end):
    local = {**article, 'content_text': article['content_text'][start:end]}
    local['keyword_candidates'] = keywords.candidates(local)
    local['material_anchors'] = material_anchors(article, start, end)
    local['condition_leads'] = condition_leads(article, start, end)
    local['segment'] = {'start': start, 'end': end, 'source_length': len(article['content_text'])}
    return local


def final_payload(article, maps, required=None):
    return {'title': article['title'], 'summary': article['summary'], 'source_code': article['source_code'],
            'published_at': article['published_at'], 'research_context': article.get('research_context'),
            'material_anchors': required or [], 'maps': maps}


def map_system(system):
    return (system + '\n只处理本段完整正文。每条事实与关键词引文和别名给出 title、summary 或 content_text 中逐字连续的原文及全篇字符区间；正文区间必须落在本段范围内。'
            '本段若明确给出影响主事件结论的评论截止日、生效日期、提案/终版状态、适用范围或实施条件，优先在facts中以各自连续原文证据保留；不能只写在summary。'
            'material_anchors是冻结原文明确标注的行动状态和日期，请优先在facts中保留；遗漏会列入质量评审，但不得为补齐而捏造引文。'
            'condition_leads是从原文抽取的限定词线索，并非已经成立的当前事实；核对其上下文，'
            '仅当涉及本文主事件的适用范围或实施条件时，在facts用完整连续原文说明，不得把历史背景写成当前要求。'
            '先写关键事实，再写检索关键词；别名与quote完全相同时不重复提交别名。原报区间正确或引文本段唯一时不要附加anchor。'
            '若短引文在本段有多处且原报区间不准，'
            '仅可提供包含该引文一次、在同字段本段唯一的连续原文anchor；不得猜位置或拼接。'
            '容量不足时先压缩重复关键词和次要背景，不把原文已明确的关键日期或条件写成未知；不得编造本段未见到的状态。'
            '提交的紧凑JSON不得超过输入map_output_limit字节。')


def final_system(system):
    return system + '\n仅用已核验的分段JSON合成单篇完整分析。所有事实和关键词引文必须逐字沿用分段的 evidence.quote；不得从未看到的原文推断新引文。优先保留已证实的状态、日期与条件；material_anchors仅作质量提示，缺失时不得伪造。'


def plan(article, provider, system):
    content = article['content_text']
    mapped_system = map_system(system)
    size = min(MAX_SOURCE_BYTES, max(1, provider.context_window - 4 * provider.max_tokens))
    while size >= 1_024:
        parts = boundaries(content, size)
        if all(fits_reserved(provider, mapped_system, map_payload(article, left, right), MapResult)
               for left, right in parts):
            break
        size //= 2
    else:
        raise ValueError('完整新闻分段无法适配模型上下文；未发送模型请求')
    final = final_system(system)
    # 双重JSON编码最坏情形是引号/反斜杠全部需要再次转义。
    map_limit = min(MAX_MAP_BYTES, MAX_TOTAL_MAP_BYTES // len(parts))
    while map_limit >= 1_024 and not fits_reserved(provider, final,
            final_payload(article, ['\\' * map_limit for _ in parts],
                          [anchor for left, right in parts for anchor in material_anchors(article, left, right)]),
            contracts.NewsAnalysisDraft):
        map_limit //= 2
    if map_limit < 1_024:
        raise ValueError('分段汇总超出模型上下文；未发送模型请求')
    planned = [(left, right, {**map_payload(article, left, right), 'map_output_limit': map_limit})
               for left, right in parts]
    if not all(fits_reserved(provider, mapped_system, payload, MapResult) for _, _, payload in planned):
        raise ValueError('分段输入超出模型上下文；未发送模型请求')
    return planned, map_limit


def validate_map(result, article, left, right, limit, *, corrections=None):
    checked = result.model_copy(deep=True)
    proofs = [(f'facts[{index}].evidence', fact.evidence) for index, fact in enumerate(checked.facts)]
    for index, word in enumerate(checked.keywords):
        proofs.append((f'keywords[{index}].quote', word.quote))
        proofs.extend((f'keywords[{index}].aliases[{alias_index}]', alias)
                      for alias_index, alias in enumerate(word.aliases))
    audit, errors, dropped_aliases = [], [], {}

    def reject(path, reason, proof, keyword_index=None, alias_index=None):
        if alias_index is None:
            errors.append(f'{path}: {reason}')
            return
        dropped_aliases.setdefault(keyword_index, set()).add(alias_index)
        audit.append({'action': 'drop_invalid_map_alias', 'path': path, 'field': proof.field,
                      'quote': proof.quote, 'original': {'start': proof.start, 'end': proof.end},
                      'reason': reason})

    for path, proof in proofs:
        alias_ref = re.fullmatch(r'keywords\[(\d+)\]\.aliases\[(\d+)\]', path)
        alias_key = int(alias_ref.group(1)) if alias_ref else None
        alias_index = int(alias_ref.group(2)) if alias_ref else None
        field = article.get(proof.field) or ''
        start, end = (left, right) if proof.field == 'content_text' else (0, len(field))
        correct = field[proof.start:proof.end] == proof.quote and start <= proof.start < proof.end <= end
        if correct:
            match = proof.start
            used_context = False
        elif field[proof.start:proof.end] == proof.quote:
            reject(path, '引文跨越本段边界', proof, alias_key, alias_index)
            continue
        else:
            match = field.find(proof.quote, start, end)
            if match < 0:
                reject(path, '本字段/本段无连续逐字引文', proof, alias_key, alias_index)
                continue
            used_context = field.find(proof.quote, match + 1, end) >= 0
            if used_context:
                if proof.anchor is None:
                    reject(path, '本字段/本段引文多处，须提供唯一连续原文锚点', proof, alias_key, alias_index)
                    continue
                anchor_start = field.find(proof.anchor, start, end)
                if anchor_start < 0:
                    reject(path, '连续原文锚点不在本字段/本段', proof, alias_key, alias_index)
                    continue
                if field.find(proof.anchor, anchor_start + 1, end) >= 0:
                    reject(path, '连续原文锚点在本字段/本段有多处', proof, alias_key, alias_index)
                    continue
                prefix = proof.quote[:min(len(proof.quote), 16)]
                within = proof.anchor.find(prefix)
                if within < 0 or proof.anchor.find(prefix, within + 1) >= 0:
                    reject(path, '引文开头在锚点内缺失或重复', proof, alias_key, alias_index)
                    continue
                match = anchor_start + within
        correct_end = match + len(proof.quote)
        if not start <= match < correct_end <= end or field[match:correct_end] != proof.quote:
            reject(path, '引文跨越本段边界或与冻结原文不符', proof, alias_key, alias_index)
            continue
        if (proof.start, proof.end) != (match, correct_end) or proof.anchor is not None:
            correction = {'path': path, 'field': proof.field,
                          'original': {'start': proof.start, 'end': proof.end},
                          'corrected': {'start': match, 'end': correct_end}}
            if proof.anchor is not None:
                correction['anchor'] = proof.anchor
                correction['reason'] = 'context_match' if used_context else 'redundant_anchor'
            audit.append(correction)
            proof.start, proof.end, proof.anchor = match, correct_end, None
    if errors:
        raise ValueError('；'.join(errors[:6]) + (f'；共{len(errors)}处错误' if len(errors) > 6 else ''))
    for word_index, indexes in dropped_aliases.items():
        checked.keywords[word_index].aliases = [alias for index, alias in enumerate(checked.keywords[word_index].aliases)
                                                if index not in indexes]
    size = len(map_json(checked).encode('utf-8'))
    if size > limit:
        raise ValueError(f'单段结果{size}字节，超过结构化汇总预算{limit}字节；'
                         '优先压缩重复关键词、非关键别名及次要背景事实，不删行动状态、日期或适用条件')
    if corrections is not None:
        corrections.extend(audit)
    return checked if audit else result


def validate_final(draft, article, maps):
    from alphapulse.briefings.analysis_model import normalize_draft
    output, audit = normalize_draft(draft, article)
    proofs = {fact.evidence.quote for item in maps for fact in item.facts}
    proofs.update(word.quote.quote for item in maps for word in item.keywords)
    if any(fact.quote not in proofs for fact in output.facts) or any(word.quote not in proofs for word in output.keywords):
        raise ValueError('最终引文不在已核验的分段证据中')
    return output, audit
