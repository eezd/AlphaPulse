from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as N

import pytest

@pytest.fixture
def admission(app_env):
    from alphapulse.briefings import admission
    return admission


@pytest.mark.parametrize('published,seen_offset,expected', [
    ('2026-11-15', 0, 'new_publication'), ('2026-11-14', 0, 'late_arrival'),
    ('2026-11-16', 0, 'future_date'), (None, 0, 'unknown_date'),
    ('2026-02-30', 0, 'unknown_date'), ('20261115', 0, 'unknown_date'),
    ('2026-11-15', -2, 'historical'),
])
def test_research_publication_boundary(admission, published, seen_offset, expected):
    activated = datetime(2026, 11, 14, 16, tzinfo=UTC)
    item = N(extra={'published_date': published}, first_seen_at=activated + timedelta(hours=1 + seen_offset))
    context = admission.research_context(item, N(research_enabled_at=activated))
    assert context['reason'] == expected
    assert context['mode'] == ('report' if expected == 'new_publication' else 'archive')


@pytest.mark.parametrize('status,body,mode', [('full', '有效正文', 'report'), ('full', ' \n\t', 'collect_only'),
    ('excerpt', '节选', 'collect_only'), ('metadata', '', 'collect_only'), ('extraction_failed', '', 'collect_only')])
def test_text_admission_and_retained_fulltext(admission, status, body, mode):
    now = datetime(2026, 11, 15, 1, tzinfo=UTC)
    item = N(source_code='citi_insights', extra={'published_date':'2026-11-15',
        'research':{'content_status':status, 'access_status':'forbidden'}}, content_text=body, first_seen_at=now)
    source = N(analysis_policy='automatic', research_enabled_at=now)
    assert admission.view(item, source)['mode'] == mode


@pytest.mark.parametrize('stage,version', [('analysis',5), ('period',3), ('daily',3)])
def test_reviewable_skill_matches_initializer(admission, stage, version):
    from alphapulse.briefings import contracts
    draft = Path(__file__).resolve().parents[2] / f'deploy/skills/{stage}-v{version}.md'
    # 安装包测试目录不包含 deploy；仓库验收负责逐字同步检查。
    if draft.exists():
        assert draft.read_text() == contracts.SKILL_TEMPLATES[stage][1] + '\n'
    assert admission.RESEARCH_MARKER in contracts.SKILL_TEMPLATES[stage][1]
