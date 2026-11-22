"""单篇草稿、共享修复预算和长文分段的无网络回归。"""
import asyncio
from types import SimpleNamespace as N
import pytest


def article(content='BTC 研究正文'):
    return {'title': 'BTC', 'summary': '研究', 'content_text': content, 'source_code': 'synthetic',
            'published_at': None, 'keyword_candidates': {'items': [], 'matched_count': 0, 'truncated': False}}


def draft(contracts, aliases=None, *, quote='BTC', duplicate=False):
    keyword = {'canonical': 'BTC', 'aliases': aliases if aliases is not None else ['BTC'],
               'kind': 'asset', 'quote': quote}
    return contracts.NewsAnalysisDraft(keywords=[keyword, dict(keyword)] if duplicate else [keyword],
        summary='报道摘要', facts=[{'text': '报道BTC', 'quote': 'BTC'}], impact='', importance_score=40,
        importance_reason='影响市场', primary_region={'code': 'CRYPTO', 'name': '加密货币'},
        related_regions=[], assets=[], uncertainties=[], scenarios=[])


def provider():
    return N(id=1, protocol='openai', base_url='http://127.0.0.1:1', model='synthetic',
             reasoning_effort=None, max_tokens=8192, context_window=250000)


def test_draft_removes_only_blank_duplicate_aliases_and_preserves_original(app_env):
    from alphapulse.briefings import analysis_model, contracts
    original = draft(contracts, ['BTC', '', 'BTC'], duplicate=True)
    result, audit = analysis_model.normalize_draft(original, article())
    assert len(result.keywords) == 1
    assert result.keywords[0].aliases == ['BTC']
    assert audit['draft_raw_keywords'][0]['aliases'] == ['BTC', '', 'BTC']
    assert {decision['action'] for decision in audit['draft_decisions']} == {
        'drop_blank_or_duplicate_alias', 'merge_identical_keyword'}
    assert original.keywords[0].aliases == ['BTC', '', 'BTC']
    conflicting = original.model_copy(deep=True)
    conflicting.keywords[1].quote = '研究'
    with pytest.raises(ValueError, match='类别或引文冲突'):
        analysis_model.normalize_draft(conflicting, article())
    with pytest.raises(ValueError, match='关键词无可保留|at least 1'):
        analysis_model.normalize_draft(draft(contracts, ['', ' ']), article())
    with pytest.raises(ValueError, match='事实证据'):
        invalid = draft(contracts)
        invalid.facts[0].quote = '不存在'
        analysis_model.normalize_draft(invalid, article())


def test_one_directed_repair_shared_across_submissions(app_env, monkeypatch):
    from alphapulse.ai import submission
    from alphapulse.briefings import analysis_model, contracts
    calls = []
    raw = draft(contracts, quote='不存在').model_dump()

    async def fake_complete(source, system, payload, schema, *, allow_repair=True):
        calls.append((payload, allow_repair))
        if payload.get('correction'):
            return draft(contracts), {'input_tokens': 4}
        return contracts.NewsAnalysisDraft.model_validate(raw), {'input_tokens': 3}

    monkeypatch.setattr(submission, 'complete', fake_complete)
    budget = analysis_model.Budget(provider())
    result, _ = asyncio.run(analysis_model.analyze(provider(), '', article(), budget))
    assert result.keywords[0].quote == 'BTC'
    assert budget.used and len(calls) == 2
    assert budget.usage == {'rounds': [{'input_tokens': 3}, {'input_tokens': 4}]}
    previous = budget.usage
    budget.usages.append(None)
    assert previous == {'rounds': [{'input_tokens': 3}, {'input_tokens': 4}]}
    budget.usages.pop()
    assert all(not allowed for _, allowed in calls)
    with pytest.raises(analysis_model.AnalysisFailure, match='错误仍存在'):
        asyncio.run(budget.submit('', article(), contracts.NewsAnalysisDraft,
                                  lambda _: (_ for _ in ()).throw(ValueError('错误仍存在'))))
    assert len(calls) == 3


def test_structural_failure_uses_same_one_repair_and_retains_unknown_usage(app_env, monkeypatch):
    from alphapulse.ai import submission
    from alphapulse.briefings import analysis_model, contracts
    calls = []

    async def fake_complete(source, system, payload, schema, *, allow_repair=True):
        calls.append(payload)
        if not payload.get('correction'):
            raise submission.SubmissionFailure('缺字段', {'rounds': [None]}, arguments={'keywords': []})
        return draft(contracts), {'input_tokens': 7}

    monkeypatch.setattr(submission, 'complete', fake_complete)
    budget = analysis_model.Budget(provider())
    asyncio.run(analysis_model.analyze(provider(), '', article(), budget))
    assert budget.used and len(calls) == 2
    assert budget.usage == {'rounds': [None, {'input_tokens': 7}]}


def test_full_utf8_body_is_covered_by_bounded_maps(app_env, monkeypatch):
    from alphapulse.briefings import analysis_parts
    monkeypatch.setattr(analysis_parts.keywords, 'candidates', lambda payload: {'items': [], 'matched_count': 0, 'truncated': False})
    body = '段' * 1_700_000  # 5.1 MB, near the largest observed body.
    planned, limit = analysis_parts.plan(article(body), provider(), 'synthetic skill')
    assert 1 < len(planned) <= analysis_parts.MAX_PARTS
    assert limit <= analysis_parts.MAX_MAP_BYTES
    assert len(planned) * limit <= analysis_parts.MAX_TOTAL_MAP_BYTES
    assert all(payload['map_output_limit'] == limit for _, _, payload in planned)
    position = 0
    for left, right, payload in planned:
        assert left <= position < right
        assert payload['content_text'] == body[left:right]
        assert len(payload['content_text'].encode('utf-8')) <= analysis_parts.MAX_SOURCE_BYTES
        position = right
    assert position == len(body)
    assert ''.join(body[left if index == 0 else planned[index - 1][1]:right]
                   for index, (left, right, _) in enumerate(planned)) == body
    with pytest.raises(ValueError, match='32段'):
        analysis_parts.boundaries('a' * 6_000_000, 180_000)


def test_two_map_budget_expands_only_with_final_context_preflight(app_env, monkeypatch):
    from alphapulse.briefings import analysis_parts, contracts
    monkeypatch.setattr(analysis_parts.keywords, 'candidates',
                        lambda payload: {'items': [], 'matched_count': 0, 'truncated': False})
    frozen = article('x' * 200_000)
    planned, limit = analysis_parts.plan(frozen, provider(), 'synthetic skill')
    assert len(planned) == 2 and limit == 8_192
    assert [payload['map_output_limit'] for _, _, payload in planned] == [8_192, 8_192]
    worst_case = analysis_parts.final_payload(frozen, ['\\' * limit for _ in planned])
    assert analysis_parts.fits_reserved(provider(), analysis_parts.final_system('synthetic skill'),
                                        worst_case, contracts.NewsAnalysisDraft)


def test_invalid_optional_map_aliases_are_dropped_with_audit(app_env):
    from alphapulse.briefings import analysis_parts
    frozen = article('OCC OCC is repeated.')
    title = {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}
    ambiguous = {'field': 'content_text', 'start': 1, 'end': 4, 'quote': 'OCC'}
    missing = {'field': 'content_text', 'start': 0, 'end': 7, 'quote': 'not here'}
    mapped = analysis_parts.MapResult(summary='事实', facts=[{'text': 'BTC', 'evidence': title}],
        keywords=[{'canonical': 'BTC', 'kind': 'asset', 'quote': title,
                   'aliases': [ambiguous, missing, title]}])
    original = mapped.model_dump()
    audit = []
    checked = analysis_parts.validate_map(mapped, frozen, 0, len(frozen['content_text']), 4096,
                                          corrections=audit)
    assert mapped.model_dump() == original
    assert [alias.quote for alias in checked.keywords[0].aliases] == ['BTC']
    assert [item['action'] for item in audit] == ['drop_invalid_map_alias'] * 2
    assert [item['path'] for item in audit] == ['keywords[0].aliases[0]', 'keywords[0].aliases[1]']
    assert [item['reason'] for item in audit] == [
        '本字段/本段引文多处，须提供唯一连续原文锚点', '本字段/本段无连续逐字引文']
    assert analysis_parts.validate_map(checked, frozen, 0, len(frozen['content_text']), 4096) is checked
    invalid_fact = mapped.model_copy(deep=True)
    invalid_fact.facts[0].evidence = analysis_parts.Evidence.model_validate(missing)
    with pytest.raises(ValueError, match='facts\\[0\\].evidence'):
        analysis_parts.validate_map(invalid_fact, frozen, 0, len(frozen['content_text']), 4096)


def test_map_span_and_final_quotes_are_strict(app_env):
    from alphapulse.briefings import analysis_parts, contracts
    frozen = article('x' * 200)
    proof = {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}
    mapped = analysis_parts.MapResult(summary='标题', facts=[{'text': 'BTC报道', 'evidence': proof}],
        keywords=[{'canonical': 'BTC', 'kind': 'asset', 'quote': proof, 'aliases': [proof]}])
    assert analysis_parts.validate_map(mapped, frozen, 0, 100, 4096) is mapped
    wrong = mapped.model_copy(deep=True)
    wrong.facts[0].evidence.quote = '不存在'
    with pytest.raises(ValueError, match='连续逐字引文'):
        analysis_parts.validate_map(wrong, frozen, 0, 100, 4096)
    crossing = mapped.model_copy(deep=True)
    crossing.facts[0].evidence = analysis_parts.Evidence(field='content_text', start=90, end=110, quote='x' * 20)
    with pytest.raises(ValueError, match='边界'):
        analysis_parts.validate_map(crossing, frozen, 0, 100, 4096)
    output, _ = analysis_parts.validate_final(draft(contracts), frozen, [mapped])
    assert output.facts[0].quote == 'BTC'
    with pytest.raises(ValueError, match='最终引文'):
        analysis_parts.validate_final(draft(contracts, quote='研究'), frozen, [mapped])


def test_unique_literal_offset_correction_preserves_raw_and_audits(app_env):
    from alphapulse.briefings import analysis_parts
    frozen = article('xxUnique deadline. trailing')
    raw = {'field': 'content_text', 'start': 1, 'end': 17, 'quote': 'Unique deadline.'}
    title = {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}
    mapped = analysis_parts.MapResult(summary='有截止日', facts=[{'text': '截止日', 'evidence': raw}],
        keywords=[{'canonical': 'BTC', 'kind': 'asset', 'quote': title, 'aliases': [raw]}])
    original = mapped.model_dump()
    audit = []
    checked = analysis_parts.validate_map(mapped, frozen, 0, len(frozen['content_text']), 4096,
                                          corrections=audit)
    assert mapped.model_dump() == original
    assert checked.facts[0].evidence.start == checked.keywords[0].aliases[0].start == 2
    assert checked.facts[0].evidence.end == checked.keywords[0].aliases[0].end == 18
    assert audit == [
        {'path': 'facts[0].evidence', 'field': 'content_text',
         'original': {'start': 1, 'end': 17}, 'corrected': {'start': 2, 'end': 18}},
        {'path': 'keywords[0].aliases[0]', 'field': 'content_text',
         'original': {'start': 1, 'end': 17}, 'corrected': {'start': 2, 'end': 18}}]
    assert analysis_parts.validate_map(checked, frozen, 0, len(frozen['content_text']), 4096) is checked


def test_offset_correction_rejects_ambiguous_missing_and_other_segment(app_env):
    from alphapulse.briefings import analysis_parts
    frozen = article('Unique phrase. and Unique phrase.')
    title = {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}
    def mapped(proof):
        return analysis_parts.MapResult(summary='标题', facts=[{'text': '事实', 'evidence': proof}],
            keywords=[{'canonical': 'BTC', 'kind': 'asset', 'quote': title, 'aliases': [title]}])

    wrong = {'field': 'content_text', 'start': 1, 'end': 15, 'quote': 'Unique phrase.'}
    with pytest.raises(ValueError, match='facts\\[0\\].evidence: 本字段/本段引文多处'):
        analysis_parts.validate_map(mapped(wrong), frozen, 0, len(frozen['content_text']), 4096)
    with pytest.raises(ValueError, match='本字段/本段无连续逐字引文'):
        analysis_parts.validate_map(mapped(wrong), frozen, 15, 19, 4096)
    with pytest.raises(ValueError, match='本字段/本段无连续逐字引文'):
        analysis_parts.validate_map(mapped({'field': 'summary', 'start': 0, 'end': 3, 'quote': 'Unique phrase.'}),
                                    frozen, 0, len(frozen['content_text']), 4096)
    with pytest.raises(ValueError, match='本字段/本段无连续逐字引文'):
        analysis_parts.validate_map(mapped({'field': 'content_text', 'start': 0, 'end': 3, 'quote': 'absent'}),
                                    frozen, 0, len(frozen['content_text']), 4096)


def test_unique_literal_context_disambiguates_repeated_quotes(app_env):
    from alphapulse.briefings import analysis_parts
    source = article('OCC adopted a rule. OCC is reviewing it.\n')
    repeated = {'field': 'content_text', 'start': 1, 'end': 4, 'quote': 'OCC',
                'anchor': 'OCC is reviewing it.'}
    mapped = analysis_parts.MapResult(summary='主体', facts=[],
        keywords=[{'canonical': 'OCC', 'kind': 'entity', 'quote': repeated, 'aliases': []}])
    before = mapped.model_dump()
    audit = []
    fixed = analysis_parts.validate_map(mapped, source, 0, len(source['content_text']), 4096,
                                        corrections=audit)
    assert mapped.model_dump() == before
    assert (fixed.keywords[0].quote.start, fixed.keywords[0].quote.end) == (20, 23)
    assert audit == [{'path': 'keywords[0].quote', 'field': 'content_text',
                      'original': {'start': 1, 'end': 4}, 'corrected': {'start': 20, 'end': 23},
                      'anchor': 'OCC is reviewing it.', 'reason': 'context_match'}]
    assert fixed.keywords[0].quote.anchor is None
    for bad in ('OCC adopted a rule. OCC', 'OCC is reviewing it. OCC is reviewing it.',
                'OCC is missing here.'):
        broken = mapped.model_copy(deep=True)
        broken.keywords[0].quote.anchor = bad
        with pytest.raises(ValueError, match='锚点|引文'):
            analysis_parts.validate_map(broken, source, 0, len(source['content_text']), 4096)
    source['content_text'] += ' OCC is reviewing it.'
    duplicate = mapped.model_copy(deep=True)
    with pytest.raises(ValueError, match='锚点在本字段/本段有多处'):
        analysis_parts.validate_map(duplicate, source, 0, len(source['content_text']), 4096)
    other = mapped.model_copy(deep=True)
    other.keywords[0].quote.anchor = 'OCC adopted a rule.'
    with pytest.raises(ValueError, match='锚点'):
        analysis_parts.validate_map(other, source, 20, len(source['content_text']), 4096)


def test_unique_context_prefix_locates_full_repeated_long_quote(app_env):
    from alphapulse.briefings import analysis_parts
    quote = 'Repeated long enough source quote.'
    source = article(f'Context A. {quote} Context B. {quote}')
    proof = {'field': 'content_text', 'start': 1, 'end': 1 + len(quote), 'quote': quote,
             'anchor': 'Context B. Repeated long enough'}
    raw = analysis_parts.MapResult(summary='事实', facts=[{'text': '事实', 'evidence': proof}], keywords=[])
    audit = []
    fixed = analysis_parts.validate_map(raw, source, 0, len(source['content_text']), 4096, corrections=audit)
    expected = source['content_text'].rfind(quote)
    assert fixed.facts[0].evidence.start == expected
    assert fixed.facts[0].evidence.end == expected + len(quote)
    assert fixed.facts[0].evidence.anchor is None
    assert audit[0]['original'] == {'start': 1, 'end': 1 + len(quote)}
    assert audit[0]['corrected'] == {'start': expected, 'end': expected + len(quote)}
    assert audit[0]['reason'] == 'context_match'
    raw.facts[0].evidence.anchor = 'Repeated long enough'
    with pytest.raises(ValueError, match='锚点在本字段/本段有多处'):
        analysis_parts.validate_map(raw, source, 0, len(source['content_text']), 4096)


def test_unneeded_anchor_is_removed_only_after_unique_literal_proof(app_env):
    from alphapulse.briefings import analysis_parts
    source = article('Unique deadline.')
    proof = {'field': 'content_text', 'start': 1, 'end': 17, 'quote': 'Unique deadline.',
             'anchor': 'Unrelated anchor.'}
    raw = analysis_parts.MapResult(summary='截止日', facts=[{'text': '事实', 'evidence': proof}], keywords=[])
    original = raw.model_dump()
    audit = []
    fixed = analysis_parts.validate_map(raw, source, 0, len(source['content_text']), 4096, corrections=audit)
    assert raw.model_dump() == original
    assert fixed.facts[0].evidence.model_dump(exclude_none=True) == {
        'field': 'content_text', 'start': 0, 'end': 16, 'quote': 'Unique deadline.'}
    assert audit == [{'path': 'facts[0].evidence', 'field': 'content_text',
                      'original': {'start': 1, 'end': 17}, 'corrected': {'start': 0, 'end': 16},
                      'anchor': 'Unrelated anchor.', 'reason': 'redundant_anchor'}]
    with pytest.raises(ValueError, match=r'单段结果\d+字节，超过结构化汇总预算100字节'):
        analysis_parts.validate_map(raw, source, 0, len(source['content_text']), 100)


def test_map_budget_counts_the_actual_compact_final_input(app_env):
    import json
    from alphapulse.briefings import analysis_parts
    source = article('Short source.')
    mapped = analysis_parts.MapResult(summary='s',
        facts=[{'text': 'fact', 'evidence': {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}}],
        keywords=[])
    mapped.facts[0].text += 'x' * (4096 - len(analysis_parts.map_json(mapped).encode('utf-8')))
    compact = analysis_parts.map_json(mapped)
    assert len(compact.encode('utf-8')) == 4096
    assert len(json.dumps(mapped.model_dump(exclude_none=True), ensure_ascii=False).encode('utf-8')) > 4096
    assert analysis_parts.validate_map(mapped, source, 0, len(source['content_text']), 4096) is mapped
    assert analysis_parts.final_payload(source, [compact])['maps'] == [compact]
    with pytest.raises(ValueError, match='超过结构化汇总预算4095字节'):
        analysis_parts.validate_map(mapped, source, 0, len(source['content_text']), 4095)


def test_explicit_regulatory_metadata_is_quality_hint_not_a_failure(app_env):
    from alphapulse.briefings import analysis_parts, contracts
    text = ('ACTION: Proposed rule. SUMMARY: Public consultation. '
            'DATES: Comments should be received on or before November 2, 2026. ADDRESSES: Website.')
    source = article(text)
    required = analysis_parts.material_anchors(source, 0, len(text))
    assert [(item['kind'], item['quote']) for item in required] == [
        ('action', 'Proposed rule.'),
        ('dates', 'Comments should be received on or before November 2, 2026.')]
    assert analysis_parts.map_payload(source, 0, len(text))['material_anchors'] == required
    source['content_text'] += ' Prior policy applied only if banks opted in.'
    assert analysis_parts.condition_leads(source, 0, len(source['content_text']))[0]['quote'] == 'only if'
    assert analysis_parts.map_payload(source, 0, len(source['content_text']))['condition_leads'][0]['kind'] == 'eligibility'
    title = {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}
    omitted = analysis_parts.MapResult(summary='规则征求意见，11月2日截止',
        facts=[{'text': 'BTC', 'evidence': title}], keywords=[])
    assert analysis_parts.validate_map(omitted, source, 0, len(text), 4096) is omitted
    assert analysis_parts.validate_final(draft(contracts), source, [omitted])[0].facts[0].quote == 'BTC'
    full = analysis_parts.MapResult(summary='规则征求意见，11月2日截止',
        facts=[{'text': '标题涉及BTC', 'evidence': title}] + [
            {'text': '提案', 'evidence': {'field': 'content_text', 'start': x['start'],
                                      'end': x['end'], 'quote': x['quote']}} for x in required],
        keywords=[{'canonical': 'BTC', 'kind': 'asset', 'quote': title, 'aliases': []}])
    assert analysis_parts.validate_map(full, source, 0, len(text), 4096) is full
    final = draft(contracts)
    final.facts.extend(contracts.Fact(text=item['kind'], quote=item['quote']) for item in required)
    assert analysis_parts.validate_final(final, source, [full])[0].facts[-1].quote == required[-1]['quote']


def test_metadata_gate_is_limited_to_explicit_headers(app_env):
    from alphapulse.briefings import analysis_parts
    text = 'The report discussed an effective date in an older proposal. ' * 6
    source = article(text)
    assert analysis_parts.material_anchors(source, 0, len(text)) == []
    source['content_text'] = ('ACTION: Final rule. DATES: Effective date: The rule is effective on May 3, 2027. '
                              'At the effective date, reporting will not change without further agency action. '
                              'ADDRESSES: Website.')
    expected = analysis_parts.material_anchors(source, 0, len(source['content_text']))
    assert [item['quote'] for item in expected] == [
        'Final rule.', 'The rule is effective on May 3, 2027.',
        'At the effective date, reporting will not change without further agency action.']
    assert all(source['content_text'][item['start']:item['end']] == item['quote'] for item in expected)
    source['content_text'] = 'DATES: Effective date: Final on May 3, 2027. ' + 'x' * 22_000
    assert analysis_parts.material_anchors(source, 0, 10) == []
    assert analysis_parts.material_anchors(source, 20_000, len(source['content_text'])) == []


def test_material_date_hint_does_not_relax_final_quote_proof(app_env):
    from alphapulse.briefings import analysis_parts, contracts
    mapped_system = analysis_parts.map_system('candidate')
    for field in ('截止日', '生效日期', '提案/终版状态', '实施条件', 'facts', 'summary'):
        assert field in mapped_system
    assert '长文分段时' in contracts.ANALYSIS_SKILL
    assert '评论截止日' in contracts.ANALYSIS_SKILL

    source = article('Comments should be received on or before November 2, 2026.')
    title = {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}
    mapped = analysis_parts.MapResult(summary='意见截止日是2026年11月2日',
        facts=[{'text': '标题涉及BTC', 'evidence': title}],
        keywords=[{'canonical': 'BTC', 'kind': 'asset', 'quote': title, 'aliases': [title]}])
    assert analysis_parts.validate_map(mapped, source, 0, len(source['content_text']), 4096) is mapped
    final = draft(contracts)
    final.facts.append(contracts.Fact(text='意见截止日为2026年11月2日', quote='November 2, 2026.'))
    with pytest.raises(ValueError, match='最终引文'):
        analysis_parts.validate_final(final, source, [mapped])
