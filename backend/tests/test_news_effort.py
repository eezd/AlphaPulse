"""Two-stage model overrides and bound Skills preserve shared configuration."""
import asyncio
from types import SimpleNamespace as N
import pytest


@pytest.mark.parametrize('stage', ['index', 'assign', 'research'])
@pytest.mark.parametrize('selected', [None, 'low', 'medium', 'high', 'xhigh', 'max'])
@pytest.mark.parametrize('inherited', [None, 'medium'])
def test_stage_effort_isolated(trading, stage, selected, inherited):
    from alphapulse.models import AiProvider, SkillNode
    from alphapulse.news_research.service import model_config
    from alphapulse.news_research.contracts import task_skill
    config_stage = "index" if stage == "assign" else stage
    shared = N(id=1, model='shared', reasoning_effort=inherited, max_tokens=8192, context_window=200000)
    node = N(id=1, scope='news', kind='skill', enabled=True, parent_id=None, published_version_id=1)
    version = N(version_no=1, content='## 索引规则\nSynthetic index\n## 归属规则\nSynthetic assignment')
    class Session:
        async def get(self, model, identifier):
            if model is AiProvider: return N(**vars(shared))
            return node if model is SkillNode else version
        def expunge(self, obj): pass
    config = {key: {'provider_id': 1, 'model': key+'-model', 'skill_id': 1,
                   'reasoning_effort': selected if key == config_stage else None} for key in ['index', 'research']}
    async def check():
        actual, skill, rules = await model_config(Session(), config, stage)
        assert actual.model == config_stage+'-model'
        assert actual.reasoning_effort == (selected if selected is not None else inherited)
        assert rules['model_config']['selected_reasoning_effort'] == selected
        assert rules['model_config']['reasoning_effort'] == actual.reasoning_effort
        assert rules['content'] == version.content and skill.endswith(task_skill(version.content, stage))
        other = 'research' if config_stage == 'index' else 'index'
        assert (await model_config(Session(), config, other))[0].reasoning_effort == inherited
        assert shared.model == 'shared' and shared.reasoning_effort == inherited
    asyncio.run(check())


@pytest.mark.parametrize('stage', ['index', 'research'])
def test_effort_validation_and_default(trading, stage):
    from pydantic import ValidationError
    from alphapulse.news_research.api import ModelConfig
    config = {'provider_id': 1, 'model': stage, 'skill_id': 1}
    assert ModelConfig(**config).reasoning_effort is None
    with pytest.raises(ValidationError): ModelConfig(**config, reasoning_effort='unsupported')


@pytest.mark.parametrize('scope,enabled,published', [('shared', True, 1), ('news', False, 1), ('news', True, None)])
def test_unpublished_disabled_and_shared_skills_never_reach_model(trading, scope, enabled, published):
    from alphapulse.models import AiProvider, SkillNode
    from alphapulse.news_research.service import model_config
    class Session:
        async def get(self, model, identifier):
            if model is AiProvider: return N(id=1, model='shared', reasoning_effort=None)
            assert model is SkillNode
            return N(scope=scope, kind='skill', enabled=enabled, published_version_id=published, parent_id=None)
        def expunge(self, obj): pass
    with pytest.raises(ValueError):
        asyncio.run(model_config(Session(), {'index': {'provider_id': 1, 'model': 'index', 'skill_id': 1}}, 'index'))
