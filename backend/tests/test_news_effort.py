"""News stage overrides and template refreshes preserve shared state."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


@pytest.mark.parametrize('stage,field', [('grouping', 'reasoning_effort'), ('analysis', 'second_reasoning_effort'), ('outlook', 'third_reasoning_effort')])
@pytest.mark.parametrize('selected', [None, 'low', 'medium', 'high', 'xhigh', 'max'])
@pytest.mark.parametrize('inherited', [None, 'medium'])
def test_stage_effort_isolated_and_fingerprinted(trading, stage, field, selected, inherited):
    from alphapulse.models import AiProvider, SkillNode
    from alphapulse.news_risk import pipeline
    provider = SimpleNamespace(id=1, model='shared', reasoning_effort=inherited, max_tokens=8192, context_window=200000)
    node = SimpleNamespace(id=1, scope='news', kind='skill', enabled=True, parent_id=None, published_version_id=1)
    version = SimpleNamespace(version_no=1, content='Synthetic rules')
    class Session:
        async def get(self, model, identifier):
            if model is AiProvider: return SimpleNamespace(**vars(provider))
            return node if model is SkillNode else version
        def expunge(self, obj): pass
    settings = SimpleNamespace(provider_id=1, second_provider_id=1, third_provider_id=1, model='luna', second_model='review', third_model='advanced',
        grouping_skill_id=1, analysis_skill_id=1, outlook_skill_id=1, reasoning_effort=None, second_reasoning_effort=None, third_reasoning_effort=None)
    async def check():
        _, _, _, before = await pipeline.configuration(Session(), settings, stage)
        setattr(settings, field, selected)
        actual, _, rules, fingerprint = await pipeline.configuration(Session(), settings, stage)
        assert actual.reasoning_effort == (selected if selected is not None else inherited)
        assert rules['model_config']['selected_reasoning_effort'] == selected
        assert rules['model_config']['reasoning_effort'] == actual.reasoning_effort
        assert (before == fingerprint) is (selected is None)
        for other in {'grouping', 'analysis', 'outlook'} - {stage}:
            assert (await pipeline.configuration(Session(), settings, other))[0].reasoning_effort == inherited
        assert provider.model == 'shared' and provider.reasoning_effort == inherited
    asyncio.run(check())


@pytest.mark.parametrize('field', ['reasoning_effort', 'second_reasoning_effort', 'third_reasoning_effort'])
def test_effort_validation_and_default(trading, field):
    from pydantic import ValidationError
    from alphapulse.api.risk import SettingsIn
    assert getattr(SettingsIn(), field) is None
    with pytest.raises(ValidationError): SettingsIn(**{field: 'unsupported'})


def test_template_refresh_appends_only_changed_drafts_and_preserves_publication(trading, monkeypatch):
    from alphapulse.ai import skills
    from alphapulse.api import risk
    from alphapulse.models import SkillNode
    from alphapulse.news_risk.pipeline import TEMPLATES
    nodes = [SimpleNamespace(id=i, name=name, scope='news', kind='skill', published_version_id=100+i) for i, (name, _) in enumerate(TEMPLATES.values(), 1)]
    versions = {n.id: [SimpleNamespace(content='User draft', version_no=1)] for n in nodes}
    class Session:
        def __init__(self): self.current = None
        async def scalar(self, statement):
            if statement.column_descriptions[0]['entity'] is SkillNode:
                name = statement.compile().params['name_1']
                self.current = next(n for n in nodes if n.name == name)
                return self.current
            return versions[self.current.id][-1]
        commit = AsyncMock()
    async def add(session, node, content, source, note):
        versions[node.id].append(SimpleNamespace(content=content, version_no=len(versions[node.id])+1))
    monkeypatch.setattr(skills, 'add_version', add)
    async def check():
        session = Session()
        original = [n.published_version_id for n in nodes]
        first = await risk.templates(session)
        assert (await risk.templates(session, risk.TemplatesIn())).data == first.data
        assert all(len(v) == 1 for v in versions.values())
        refreshed = await risk.templates(session, risk.TemplatesIn(refresh_existing=True))
        assert refreshed.data == first.data
        assert all(len(v) == 2 and v[0].content == 'User draft' for v in versions.values())
        await risk.templates(session, risk.TemplatesIn(refresh_existing=True))
        assert all(len(v) == 2 for v in versions.values())
        assert [n.published_version_id for n in nodes] == original
    asyncio.run(check())


@pytest.mark.parametrize('scope,kind', [('shared', 'skill'), ('news', 'folder')])
def test_template_refresh_rejects_name_conflict(trading, scope, kind):
    from fastapi import HTTPException
    from alphapulse.api import risk
    session = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(scope=scope, kind=kind)), commit=AsyncMock())
    with pytest.raises(HTTPException) as error:
        asyncio.run(risk.templates(session, risk.TemplatesIn(refresh_existing=True)))
    assert error.value.status_code == 409
    session.commit.assert_not_awaited()
