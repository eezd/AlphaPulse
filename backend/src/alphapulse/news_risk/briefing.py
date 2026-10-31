"""Complete batched reading lists and version-bound cross-domain synthesis."""
import hashlib
import json
from pydantic import BaseModel, Field
from alphapulse.news_risk.service import complete, fits_context, OutputTruncated

class Reading(BaseModel):
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
        result.append({'id': package['id'], 'importance_stars': package['importance_stars'], 'result': content})
    return sorted(result, key=lambda p: p['id'])


async def generate(provider, system, inputs):
    reading, notes, usages = [], [], []
    pending = list(inputs)
    while pending:
        batch = []
        while pending and fits_context(provider, system, {'task': 'ranking', 'packages': [*batch, pending[0]]}, Ranked):
            batch.append(pending.pop(0))
        if not batch: raise ValueError('单个包超出高级模型上下文，未省略输入')
        batches = [batch]
        while batches:
            chunk = batches.pop(0)
            try:
                ranked, used = await complete(provider, system, {'task': 'ranking', 'packages': chunk}, Ranked)
            except OutputTruncated as error:
                if error.usage is not None: usages.append(error.usage)
                if len(chunk) == 1:
                    raise OutputTruncated('单包排序输出被截断，请增大模型最大输出配置', error.usage) from None
                middle = len(chunk)//2
                batches[0:0] = [chunk[:middle], chunk[middle:]]
                continue
            expected = {(p['id'], p['revision']) for p in chunk}
            actual = [(r.id, r.revision) for r in ranked.reading]
            if len(actual) != len(set(actual)) or set(actual) != expected:
                raise ValueError('阅读排序遗漏、重复或引用未知包版本')
            reading.extend(r.model_dump() for r in ranked.reading)
            notes.append(ranked.notes); usages.append(used)
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
    allowed = {(p['id'], p['revision']) for p in inputs}
    if any((r.id, r.revision) not in allowed for r in synthesis.references): raise ValueError('综合判断引用未知包版本')
    usages.append(used)
    reading.sort(key=lambda r: (-r['priority'], -next(p['importance_stars'] for p in inputs if p['id'] == r['id']), r['id']))
    return {'reading': reading, 'synthesis': synthesis.model_dump(), 'usage': usages}
