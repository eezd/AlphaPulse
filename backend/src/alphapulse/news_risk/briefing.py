"""Complete batched reading lists and version-bound cross-domain synthesis."""
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, Field, ValidationError
from alphapulse.news_risk.service import complete, fits_context, OutputTruncated, SubmissionFailure

class Reading(BaseModel):
    kind: Literal['package', 'event'] = 'package'
    id: int
    revision: int
    priority: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1)

class Ranked(BaseModel):
    reading: list[Reading]
    notes: str = Field(min_length=1)

class Trend(BaseModel):
    summary: str = Field(min_length=1)
    conditions: str = Field(min_length=1)
    counter_evidence: str = Field(min_length=1)
    watchpoints: str = Field(min_length=1)

class Synthesis(BaseModel):
    summary: str = Field(min_length=1)
    market: str = Field(min_length=1)
    short_term: Trend
    medium_term: Trend
    references: list[Reading]

class Notes(BaseModel):
    notes: str = Field(min_length=1)


def fingerprint(inputs, rules):
    return hashlib.sha256(json.dumps([inputs, rules], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def semantic_packages(inputs):
    result = []
    for package in inputs:
        content = {k: v for k, v in package['result'].items() if k != 'evidence_refs'}
        if 'claims' in content:
            content['claims'] = [{k: v for k, v in claim.items() if k != 'evidence_refs'} for claim in content['claims']]
        result.append({'kind': package.get('kind', 'package'), 'id': package['id'], 'importance_stars': package['importance_stars'], 'result': content})
    return sorted(result, key=lambda p: (p['kind'], p['id']))


def shortlist(inputs):
    groups = {}
    for item in sorted(inputs, key=lambda p: (p['importance_stars'], str(p.get('updated_at', '')), -p['id']), reverse=True):
        groups.setdefault(item['category'], []).append(item)
    result = []
    while groups and len(result) < 12:
        for category in list(groups):
            result.append(groups[category].pop(0))
            if not groups[category]: del groups[category]
            if len(result) == 12: break
    return result


async def rank_batch(provider, system, chunk):
    expected = [{'kind': p.get('kind', 'package'), 'id': p['id'], 'revision': p['revision']} for p in chunk]
    identities = {(p['kind'], p['id'], p['revision']) for p in expected}
    payload = {'task': 'ranking', 'packages': chunk, 'expected': expected}
    usages = []
    for attempt in range(2):
        try:
            ranked, used = await complete(provider, system, payload, Ranked)
        except OutputTruncated as error:
            if usages:
                raise OutputTruncated(str(error), {'rounds': [*usages, error.usage]}) from error
            raise
        except SubmissionFailure as error:
            cause = error.__cause__
            if error.correction_attempted or not isinstance(cause, ValidationError) or not all(
                len(e['loc']) >= 3 and e['loc'][0] == 'reading' and e['loc'][2] in ('kind', 'id', 'revision')
                for e in cause.errors()):
                raise SubmissionFailure(str(error), {'rounds': [*usages, error.usage]}) from error
            usages.append(error.usage)
            detail = cause.errors(include_input=False, include_url=False, include_context=False)
        else:
            usages.append(used)
            actual = [(r.kind, r.id, r.revision) for r in ranked.reading]
            if len(actual) == len(set(actual)) and set(actual) == identities:
                return ranked, usages
            detail = {'missing': sorted(identities-set(actual)), 'unknown': sorted(set(actual)-identities),
                'duplicates': [r for r in set(actual) if actual.count(r) > 1]}
        if attempt or any(isinstance(u, dict) and len(u.get("rounds", [])) > 1 for u in usages):
            raise SubmissionFailure('阅读排序遗漏、重复或引用未知包版本：'+json.dumps(detail, ensure_ascii=False), {'rounds': usages})
        payload = {**payload, 'correction': {'errors': detail,
            'instruction': '仅重新提交当前批次的完整排序；逐项复制expected中的kind、id、revision，不遗漏、不重复。'}}


async def generate(provider, system, inputs, ranking_only=False, rankings=None):
    reading, notes, usages = [], [], []
    pending = list(inputs) if rankings is None else []
    if rankings is not None:
        reading = rankings
        by_id = {(r.get('kind', 'package'), r['id'], r['revision']): r for r in rankings}
        notes = [json.dumps({'package': p, 'ranking': by_id[(p.get('kind', 'package'), p['id'], p['revision'])]},
            ensure_ascii=False, default=str) for p in inputs]
    while pending:
        batch = []
        while pending and len(batch) < 12 and fits_context(provider, system, {'task': 'ranking', 'packages': [*batch, pending[0]],
            'expected': [{'kind': p.get('kind', 'package'), 'id': p['id'], 'revision': p['revision']} for p in [*batch, pending[0]]]}, Ranked):
            batch.append(pending.pop(0))
        if not batch: raise ValueError('单个包超出高级模型上下文，未省略输入')
        batches = [batch]
        while batches:
            chunk = batches.pop(0)
            try:
                ranked, used = await rank_batch(provider, system, chunk)
            except OutputTruncated as error:
                if error.usage is not None: usages.append(error.usage)
                if len(chunk) == 1:
                    raise OutputTruncated('单包排序输出被截断，请增大模型最大输出配置', {'rounds': usages}) from None
                middle = len(chunk)//2
                batches[0:0] = [chunk[:middle], chunk[middle:]]
                continue
            except SubmissionFailure as error:
                raise SubmissionFailure(str(error), {'completed_batches': usages, 'failed_batch': error.usage}) from error
            reading.extend(r.model_dump() for r in ranked.reading)
            notes.append(ranked.notes); usages.extend(used)
    if ranking_only:
        return {'reading': sorted(reading, key=lambda r: (-r['priority'], r['id'])), 'usage': usages}
    payload = {'task': 'synthesis', 'notes': notes}
    while not fits_context(provider, system, payload, Synthesis):
        compacted, group = [], []
        for note in notes:
            if not fits_context(provider, system, {'task': 'synthesis_notes', 'notes': [*group, note]}, Notes):
                if not group: raise ValueError('议题笔记超出高级模型上下文')
                result, used = await complete(provider, system, {'task': 'synthesis_notes', 'notes': group}, Notes)
                compacted.append(result.notes); usages.append(used); group = []
            if not fits_context(provider, system, {'task': 'synthesis_notes', 'notes': [note]}, Notes):
                raise ValueError('议题笔记超出高级模型上下文')
            group.append(note)
        if group:
            result, used = await complete(provider, system, {'task': 'synthesis_notes', 'notes': group}, Notes)
            compacted.append(result.notes); usages.append(used)
        if len(json.dumps(compacted)) >= len(json.dumps(notes)): raise ValueError('议题归纳未缩减上下文')
        notes = compacted; payload['notes'] = notes
    synthesis, used = await complete(provider, system, payload, Synthesis)
    allowed = {(p.get('kind', 'package'), p['id'], p['revision']) for p in inputs}
    if any((r.kind, r.id, r.revision) not in allowed for r in synthesis.references): raise ValueError('综合判断引用未知包版本')
    authoritative = {(r.get('kind', 'package'), r['id'], r['revision']): r for r in reading}
    synthesis.references = [Reading.model_validate(authoritative[(r.kind, r.id, r.revision)]) for r in synthesis.references]
    usages.append(used)
    reading.sort(key=lambda r: (-r['priority'], -next(p['importance_stars'] for p in inputs if p['id'] == r['id'] and p.get('kind', 'package') == r['kind']), r['id']))
    return {'reading': reading, 'synthesis': synthesis.model_dump(), 'usage': usages}
