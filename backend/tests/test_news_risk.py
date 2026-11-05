"""Risk evidence, identity and model boundaries use synthetic inputs only."""
import asyncio
import pytest


@pytest.fixture
def risk(trading):
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
