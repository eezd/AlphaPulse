"""Time repairs use synthetic completions and never call a provider."""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from alphapulse.news_research.time_contracts import PackageTimePoint


class Result(BaseModel):
    timeline_points: list[PackageTimePoint]


@pytest.mark.parametrize('outcome', ['success', 'invalid', 'query', 'truncated', 'context'])
def test_time_repair_is_bounded_and_accounts_for_calls(app_env, monkeypatch, outcome):
    from alphapulse.news_research import model as service
    from alphapulse.ai.llm import Completion
    payload = {'timeline_points': [{'occurred_at': None, 'time_precision': 'day', 'time_text': '',
        'reported_at': None, 'description': 'Unknown occurrence time', 'verification': 'insufficient',
        'evidence_refs': [{'article_id': 1, 'quote': 'Source'}]}]}
    calls = []

    async def stream(provider, system, messages, tools, effort):
        calls.append((messages, tools, effort))
        if len(calls) == 2 and outcome == 'success':
            payload['timeline_points'][0]['time_precision'] = 'unknown'
        yield Completion(tool_calls=[{'id': str(len(calls)), 'name': 'read_news' if len(calls) == 2 and outcome == 'query' else 'submit', 'arguments': payload}],
            usage={'input_tokens': 10}, truncated=len(calls) == 2 and outcome == 'truncated')

    monkeypatch.setattr(service.llm, 'stream', stream)
    provider = SimpleNamespace(model='synthetic', reasoning_effort='low', max_tokens=100, context_window=200000)
    if outcome == 'context':
        fits = iter([True, False])
        monkeypatch.setattr(service, 'fits_context', lambda *args: next(fits))
    if outcome == 'success':
        result, usage = asyncio.run(service.complete(provider, 'rules', {}, Result))
        assert result.timeline_points[0].occurred_at is None
    else:
        with pytest.raises(service.SubmissionFailure) as error:
            asyncio.run(service.complete(provider, 'rules', {}, Result))
        usage = error.value.usage
    assert len(calls) == (1 if outcome == 'context' else 2)
    assert len(usage['rounds']) == len(calls)
    if len(calls) == 2:
        assert [tool.name for tool in calls[1][1]] == ['submit']
        assert calls[1][2] == 'low'
        assert 'timeline_points' in calls[1][0][-1]['result']


def test_unrelated_validation_does_not_retry(app_env, monkeypatch):
    from alphapulse.news_research import model as service
    from alphapulse.ai.llm import Completion
    calls = []
    async def stream(*args):
        calls.append(args)
        yield Completion(tool_calls=[{'id': '1', 'name': 'submit', 'arguments': {'timeline_points': 'invalid'}}], usage={'output_tokens': 4})
    monkeypatch.setattr(service.llm, 'stream', stream)
    provider = SimpleNamespace(model='synthetic', reasoning_effort='low', max_tokens=100, context_window=200000)
    with pytest.raises(service.SubmissionFailure):
        asyncio.run(service.complete(provider, 'rules', {}, Result))
    assert len(calls) == 1


def test_research_time_repair_preserves_round_budget_and_thinking(app_env, monkeypatch):
    from alphapulse.news_research import model as service
    from alphapulse.news_research.tools import Research
    from alphapulse.ai.llm import Completion
    calls = []
    args = {'timeline_points': [{'occurred_at': None, 'time_precision': 'day', 'time_text': '', 'reported_at': None,
        'description': 'Source', 'verification': 'insufficient', 'evidence_refs': [{'quote': 'Source'}]}]}
    async def stream(provider, system, messages, tools, effort):
        calls.append((messages, tools))
        if len(calls) <= 8:
            yield Completion(tool_calls=[{'id': str(len(calls)), 'name': 'unknown_tool', 'arguments': {}}], usage={'output_tokens': 1})
        else:
            if len(calls) == 10:
                assert messages[-2]['thinking_blocks'] == [{'type': 'thinking', 'thinking': 'synthetic'}]
                assert [tool.name for tool in tools] == ['submit']
                args['timeline_points'][0]['time_precision'] = 'unknown'
            yield Completion(tool_calls=[{'id': str(len(calls)), 'name': 'submit', 'arguments': args}],
                usage={'output_tokens': 2}, thinking_blocks=[{'type': 'thinking', 'thinking': 'synthetic'}])
    monkeypatch.setattr(service.llm, 'stream', stream)
    provider = SimpleNamespace(model='synthetic', reasoning_effort='high', max_tokens=100, context_window=200000)
    research = Research()
    result, usage = asyncio.run(research.run(provider, 'rules', {}, Result))
    assert result.timeline_points[0].time_precision == 'unknown'
    assert len(calls) == len(usage['rounds']) == 10
    assert len(research.records) == 8


def usage_tokens(usage):
    if isinstance(usage, dict):
        return usage.get('input_tokens', 0) + sum(usage_tokens(v) for k, v in usage.items() if k != 'input_tokens')
    if isinstance(usage, list):
        return sum(usage_tokens(v) for v in usage)
    return 0


@pytest.mark.parametrize('failure', ['second_call', 'evidence'])
def test_extraction_failure_keeps_completed_and_failed_call_usage(app_env, monkeypatch, failure):
    from unittest.mock import AsyncMock
    from alphapulse.news_research import pipeline, model as service
    extract = pipeline.EvidenceSelection(facts='Source', evidence_ids=[0 if failure == 'second_call' else 999])
    complete = AsyncMock(side_effect=[(extract, {'input_tokens': 10}), service.SubmissionFailure('synthetic failure', {'input_tokens': 40})])
    monkeypatch.setattr(pipeline, 'complete', complete)
    monkeypatch.setattr(pipeline, 'fits_context', lambda *args: True)
    articles = [{'id': 1, 'title': 'Source' if failure == 'second_call' else 'Different'}, {'id': 2, 'title': 'Source'}]
    with pytest.raises(service.SubmissionFailure) as error:
        asyncio.run(pipeline.extract_articles(SimpleNamespace(), 'rules', articles))
    assert usage_tokens(error.value.usage) == (50 if failure == 'second_call' else 10)
    assert complete.await_count == (2 if failure == 'second_call' else 1)


@pytest.mark.parametrize('failure', ['second_call', 'evidence', 'no_shrink'])
def test_compaction_failure_keeps_every_paid_call(app_env, monkeypatch, failure):
    from unittest.mock import AsyncMock
    from alphapulse.news_research import pipeline, model as service
    group = {'facts': 'Very long source fact ' * 50, 'evidence_refs': [{'article_id': 1, 'quote': 'Source'}]}
    extract = pipeline.EvidenceSelection(facts='Source' if failure != 'no_shrink' else 'Longer' * 1000,
        evidence_ids=[999 if failure == 'evidence' else 0])
    complete = AsyncMock(side_effect=[(extract, {'input_tokens': 10}), service.SubmissionFailure('synthetic failure', {'input_tokens': 40})])
    monkeypatch.setattr(pipeline, 'complete', complete)
    monkeypatch.setattr(pipeline, 'fits_context', lambda *args: True)
    with pytest.raises(service.SubmissionFailure) as error:
        asyncio.run(pipeline.compact_extractions(SimpleNamespace(), 'rules', {'extractions': [group]}, pipeline.ReviewResult, fits=lambda *args: False))
    assert usage_tokens(error.value.usage) == (50 if failure == 'second_call' else 10)
    assert complete.await_count == (2 if failure == 'second_call' else 1)




@pytest.mark.parametrize('error_kind', ['missing', 'extra'])
def test_structural_repair_submits_full_result_once(app_env, monkeypatch, error_kind):
    from pydantic import ConfigDict
    from alphapulse.news_research import model as service
    from alphapulse.ai.llm import Completion
    class Category(BaseModel):
        model_config = ConfigDict(extra='forbid')
        category: str
    calls = []
    async def stream(provider, system, messages, tools, effort):
        calls.append(messages)
        args = ({'category': '能源'} if error_kind == 'extra' else {}) if len(calls) == 1 else {'category': '能源'}
        if len(calls) == 1 and error_kind == 'extra': args['wrong_level'] = 'reason'
        yield Completion(tool_calls=[{'id': str(len(calls)), 'name': 'submit', 'arguments': args}], usage={'input_tokens': 7})
    monkeypatch.setattr(service.llm, 'stream', stream)
    provider = SimpleNamespace(model='synthetic', reasoning_effort='max', max_tokens=100, context_window=200000)
    result, usage = asyncio.run(service.complete(provider, 'rules', {}, Category))
    assert result.category == '能源' and len(calls) == 2
    assert len(usage['rounds']) == 2
    assert 'errors' in calls[1][-1]['result']
    calls.clear()
    with pytest.raises(service.SubmissionFailure):
        asyncio.run(service.complete(provider, 'rules', {'correction': 'already corrected'}, Category))
    assert len(calls) == 1
