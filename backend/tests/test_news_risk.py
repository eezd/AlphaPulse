"""Risk evidence, identity and model boundaries use synthetic inputs only."""
import asyncio
import pytest


@pytest.fixture
def risk(app_env):
    from alphapulse.news_research import model as service
    return service


def test_truncated_completion_is_not_a_safe_label(risk, monkeypatch):
    from alphapulse.news_research.contracts import IndexResult
    from types import SimpleNamespace
    async def stream(*args):
        yield risk.llm.Completion(truncated=True, usage={'output_tokens': 100})
    monkeypatch.setattr(risk.llm, 'stream', stream)
    with pytest.raises(risk.OutputTruncated, match='截断') as exc:
        asyncio.run(risk.complete(SimpleNamespace(max_tokens=100, context_window=10000, reasoning_effort=None), '', {}, IndexResult))
    assert exc.value.usage == {'output_tokens': 100}


def test_model_override_never_changes_shared_provider(risk):
    from types import SimpleNamespace
    provider = SimpleNamespace(model='original')
    class Session:
        async def get(self, *args): return provider
        def expunge(self, obj): self.detached = obj.model
    session = Session()
    result = asyncio.run(risk.provider_for(session, SimpleNamespace(provider_id=1, model='LUNA')))
    assert session.detached == 'original' and result.model == 'LUNA'


def test_context_budget_includes_unicode_and_tool_schema(risk):
    from alphapulse.news_research.contracts import IndexResult
    from types import SimpleNamespace
    provider = SimpleNamespace(max_tokens=256, context_window=3500)
    assert not risk.fits_context(provider, '只使用原文证据', {'content': '风'*1000}, IndexResult)


@pytest.mark.parametrize('context_window', [250000, 1000000])
def test_news_context_is_capped_at_250k_including_output(risk, monkeypatch, context_window):
    from pydantic import BaseModel
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    class Result(BaseModel):
        facts: str

    provider = SimpleNamespace(max_tokens=32000, context_window=context_window)
    assert risk.fits_context(provider, '', {'content': 'x' * 200000}, Result)
    stream = AsyncMock()
    monkeypatch.setattr(risk.llm, 'stream', stream)
    with pytest.raises(ValueError, match='超出模型上下文'):
        asyncio.run(risk.complete(provider, '', {'content': 'x' * 220000}, Result))
    stream.assert_not_called()


def test_research_reserves_tool_room_within_250k_cap(risk):
    from alphapulse.news_research.tools import Research
    from pydantic import BaseModel
    from types import SimpleNamespace

    class Result(BaseModel):
        facts: str

    reader = Research()
    payload = {'content': 'x' * 190000}
    for context_window in (250000, 1000000):
        provider = SimpleNamespace(max_tokens=32000, context_window=context_window)
        assert risk.fits_context(provider, '', payload, Result)
        assert not reader.fits(provider, '', payload, Result)
        assert provider.context_window == context_window
