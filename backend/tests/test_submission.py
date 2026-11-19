"""Structured submission boundaries use synthetic inputs only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import BaseModel
import pytest


class Result(BaseModel):
    facts: str


@pytest.fixture
def submission(app_env):
    from alphapulse.ai import submission
    return submission


def test_truncated_completion_preserves_usage(submission, monkeypatch):
    async def stream(*args):
        yield submission.llm.Completion(truncated=True, usage={'output_tokens': 100})
    monkeypatch.setattr(submission.llm, 'stream', stream)
    with pytest.raises(submission.OutputTruncated, match='截断') as exc:
        asyncio.run(submission.complete(SimpleNamespace(max_tokens=100, context_window=10000,
                                                       reasoning_effort=None), '', {}, Result))
    assert exc.value.usage == {'output_tokens': 100}


def test_context_budget_includes_unicode_and_tool_schema(submission):
    provider = SimpleNamespace(max_tokens=256, context_window=3000)
    assert not submission.fits_context(provider, '只使用原文证据', {'content': '风'*1000}, Result)


@pytest.mark.parametrize('context_window', [250000, 1000000])
def test_context_is_capped_at_250k_including_output(submission, monkeypatch, context_window):
    provider = SimpleNamespace(max_tokens=32000, context_window=context_window)
    assert submission.fits_context(provider, '', {'content': 'x' * 200000}, Result)
    stream = AsyncMock()
    monkeypatch.setattr(submission.llm, 'stream', stream)
    with pytest.raises(ValueError, match='超出模型上下文'):
        asyncio.run(submission.complete(provider, '', {'content': 'x' * 220000}, Result))
    stream.assert_not_called()
