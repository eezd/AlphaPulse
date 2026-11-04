"""Acceptance guards must fail closed without models or production access."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest


@pytest.fixture
def scenario(trading, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts'))
    import accept_news_scenario
    return accept_news_scenario


def manifest():
    return {'news': [{'id': i, 'published_at': f'2025-02-{i:02}T00:00:00+00:00'} for i in range(1, 13)],
            'batches': [[i] for i in range(1, 13)], 'baseline_requests': 199, 'additional_limit': 120, 'official_sources': [{'url': 'https://one.invalid'}, {'url': 'https://two.invalid'}]}


@pytest.mark.parametrize('failure', ['duplicate', 'order', 'budget'])
def test_manifest_rejects_ambiguous_or_changed_scope(scenario, failure):
    value = manifest()
    scenario.validate_manifest(value)
    if failure == 'duplicate':
        value['batches'][-1] = [1]
    elif failure == 'order':
        value['batches'].reverse()
    else:
        value['additional_limit'] = 121
    with pytest.raises(ValueError):
        scenario.validate_manifest(value)


def test_budget_is_shared_atomic_and_resume_does_not_reset(scenario, tmp_path):
    from accept_news_research import Budget, BudgetExceeded
    path = tmp_path / 'count'
    path.write_text('199')
    budget = Budget(path, limit=205)
    def take(_):
        try:
            budget.take()
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(take, range(20))) == 6
    assert path.read_text() == '205'
    with pytest.raises(BudgetExceeded, match='205'):
        Budget(path, limit=205).take()


def test_extended_request_limit_requires_explicit_resume_finish(scenario):
    from types import SimpleNamespace
    def args(resume=False, finish=False, limit=None):
        return SimpleNamespace(resume=resume, finish=finish, request_limit=limit)
    assert scenario.request_limit(args()) == 90
    assert scenario.request_limit(args(finish=True)) == 120
    assert scenario.request_limit(args(True, True, 180)) == 180
    for value in (args(limit=180), args(True, False, 180), args(False, True, 180), args(True, True, 119)):
        with pytest.raises(ValueError, match='追加上限'):
            scenario.request_limit(value)


def test_missing_samples_never_pass_boundary_assertions(scenario):
    value = manifest()
    value.update(must_link=[[1, 2]], must_separate=[[3, 4]], references=[[3, 1]])
    tables = {m.__tablename__: [] for m in scenario.MODELS}
    checks = scenario.assertions(value, tables, [])
    assert checks and not any(c['passed'] for c in checks)


def test_external_or_duplicate_versions_do_not_count_as_natural_revisions(scenario):
    value = manifest()
    value.update(must_link=[[1]], must_separate=[], references=[])
    tables = {m.__tablename__: [] for m in scenario.MODELS}
    tables['news_research_packages'] = [{'id': 1, 'news_id': 1, 'state': 'done'}]
    tables['news_research_memberships'] = [{'package_id': 1, 'event_id': 1}]
    versions = [{'id': i, 'event_id': 1, 'revision': i, 'state': 'done'} for i in range(1, 5)]
    tables['news_research_versions'] = copy.deepcopy(versions)
    checkpoints = [{'versions': [versions[0]]}, {'versions': [versions[0]]}]
    checks = scenario.assertions(value, tables, checkpoints)
    assert not next(c['passed'] for c in checks if c['name'] == 'three_natural_revisions')


def test_published_skill_retains_base_instructions(trading, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from alphapulse.news_research import service
    provider = SimpleNamespace(id=7, model='test', reasoning_effort='medium')
    monkeypatch.setattr(service, 'provider_for', AsyncMock(return_value=provider))
    monkeypatch.setattr(service, 'configuration', AsyncMock(return_value=(provider, 'BASE\nPUBLISHED', {'content': 'PUBLISHED', 'base': 'BASE'}, 'hash')))
    _, skill, rules = asyncio.run(service.model_config(None, {'index': {'provider_id': 7, 'model': 'test', 'skill_id': 9}}, 'index'))
    assert skill == 'BASE\nPUBLISHED' and rules['base'] == 'BASE'


def test_pinned_reference_requires_later_natural_update_without_consumer_rerun(scenario):
    value = manifest()
    value.update(must_link=[[1]], must_separate=[], references=[[2, 1]])
    target = {'id': 1, 'event_id': 1, 'revision': 1, 'state': 'done'}
    consumer = {'id': 3, 'event_id': 2, 'revision': 1, 'state': 'done'}
    update = {'id': 2, 'event_id': 1, 'revision': 2, 'state': 'done'}
    tables = {m.__tablename__: [] for m in scenario.MODELS}
    tables.update(news_research_packages=[{'id': 1, 'news_id': 1, 'state': 'done'}, {'id': 2, 'news_id': 2, 'state': 'done'}],
                  news_research_memberships=[{'package_id': 1, 'event_id': 1}, {'package_id': 2, 'event_id': 2}],
                  news_research_events=[{'id': 1, 'revision': 2}, {'id': 2, 'revision': 1}],
                  news_research_versions=[target, consumer, update],
                  news_research_references=[{'version_id': 3, 'target_version_id': 1}])
    checkpoints = [{'versions': [target, update]}]
    def passed():
        return next(c['passed'] for c in scenario.assertions(value, tables, checkpoints)
                    if c['name'] == 'pinned_reference_without_cascade')
    assert not passed()
    update['id'] = 4
    assert passed()
    tables['news_research_versions'].append({'id': 5, 'event_id': 2, 'revision': 2, 'state': 'failed'})
    assert not passed()


def test_checkpoint_is_atomic_private_and_can_be_replaced(scenario, tmp_path):
    import json
    path = tmp_path / 'state.json'
    scenario.save(path, {'batch': 0})
    scenario.save(path, {'batch': 1})
    assert json.loads(path.read_text()) == {'batch': 1}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_suffix('.pending').exists()


def test_official_evidence_requires_success_correct_event_and_cited_claim(scenario):
    tables = {m.__tablename__: [] for m in scenario.MODELS}
    value = {'external_news_ids': [1], 'official_sources': [{'url': 'https://official.invalid', 'anchors': ['Official fact']}]}
    tables['news_research_packages'] = [{'id': 10, 'news_id': 1}]
    tables['news_research_memberships'] = [{'package_id': 10, 'event_id': 2}]
    version = {'id': 3, 'event_id': 2, 'state': 'done', 'evidence': [
        {'tool': 'read_web', 'result': {'url': 'https://official.invalid/article', 'content': 'Official fact was confirmed.', 'article_id': -1}}],
        'output': {'claims': [{'verdict': 'supported', 'evidence_refs': [{'article_id': -1, 'quote': 'Official fact was confirmed.'}]}]}}
    tables['news_research_versions'] = [version]
    assert scenario.official_assertions(value, tables, [2])[0]['passed']
    for field, invalid in [('state', 'failed'), ('event_id', 99), ('output', {'claims': []})]:
        old = version[field]
        version[field] = invalid
        assert not scenario.official_assertions(value, tables, [2])[0]['passed']
        version[field] = old


def test_official_hints_are_scoped_to_event_members(scenario):
    value = {'external_news_ids': [11, 22], 'official_sources': [
        {'url': 'https://one.invalid/incident'}, {'url': 'https://two.invalid/licence'}]}
    hints = scenario.official_hints(value, {11, 33})
    assert 'https://one.invalid/incident' in hints and 'https://two.invalid/licence' not in hints
    hints = scenario.official_hints(value, {22})
    assert 'https://two.invalid/licence' in hints and 'https://one.invalid/incident' not in hints
    value['official_sources'].pop()
    with pytest.raises(ValueError):
        scenario.official_hints(value, {11})


def test_reference_api_assertion_checks_changed_and_original_version(scenario, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research import api
    tables = {m.__tablename__: [] for m in scenario.MODELS}
    tables.update(news_research_packages=[{'id': 1, 'news_id': 11}, {'id': 2, 'news_id': 22}],
                  news_research_memberships=[{'package_id': 1, 'event_id': 1}, {'package_id': 2, 'event_id': 2}],
                  news_research_versions=[{'id': 5, 'event_id': 1}, {'id': 4, 'event_id': 2}],
                  news_research_references=[{'version_id': 5, 'target_version_id': 4}])
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    monkeypatch.setattr(scenario, 'SessionLocal', Session)
    read = AsyncMock(return_value=[{'version_id': 4, 'changed': True}])
    monkeypatch.setattr(api, 'version_references', read)
    manifest = {'references': [[11, 22]]}
    assert asyncio.run(scenario.reference_api_assertion(manifest, tables))[0]['passed']
    read.return_value = [{'version_id': 4, 'changed': False}]
    assert not asyncio.run(scenario.reference_api_assertion(manifest, tables))[0]['passed']
    read.return_value = [{'version_id': 99, 'changed': True}]
    assert not asyncio.run(scenario.reference_api_assertion(manifest, tables))[0]['passed']


def test_restore_rejects_changed_versions_and_incomplete_batches(scenario):
    import restore_news_scenario as recovery
    value = manifest()
    for row in value['news']:
        row['content_hash'] = 'frozen'
    tables = {m.__tablename__: [] for m in scenario.MODELS}
    tables['news_research_packages'] = [{'id': 1, 'news_id': 1, 'state': 'done', 'content_hash': 'frozen'}]
    version = {'id': 1, 'event_id': 1, 'revision': 1, 'state': 'done', 'input': {'articles': [{'id': 1}]}}
    tables['news_research_versions'] = [version]
    report = {'manifest': value, 'tables': copy.deepcopy(tables), 'checkpoints': [{'versions': copy.deepcopy([version])}]}
    _, originals, batch, _ = recovery.prepare(report, tables)
    assert len(originals) == 1 and batch == 1
    version['revision'] += 1
    with pytest.raises(ValueError, match='版本'):
        recovery.prepare(report, tables)
    version['revision'] -= 1
    tables['news_research_packages'][0]['state'] = 'assign_pending'
    with pytest.raises(ValueError, match='批次'):
        recovery.prepare(report, tables)


def test_restore_preserves_extended_cap_and_never_rewinds_counter(scenario):
    import restore_news_scenario as recovery
    report = {'manifest': manifest(), 'new_cap': 180, 'requests_new': 147}
    assert recovery.counter_limit(report, 147) == 180
    for count in (146, 181):
        with pytest.raises(ValueError, match='请求账本'):
            recovery.counter_limit(report, count)
    report['new_cap'] = 119
    with pytest.raises(ValueError, match='累计上限'):
        recovery.counter_limit(report, 147)
