"""词表消歧、证据边界和旧结果检索的回归；不调用模型或数据库。"""
import copy
import json
from pathlib import Path

import pytest

from alphapulse.briefings import contracts, keywords


def result_for(body, words=None, quote=None):
    return contracts.NewsAnalysisResult(
        keywords=words or [{'canonical': '研究', 'aliases': ['研究'], 'kind': 'topic', 'quote': '研究'}],
        summary='研究结论', facts=[{'text': '来源判断', 'quote': quote or body}],
        impact='有条件的影响', importance_score=65, importance_reason='领域影响，尚无重大范围变化',
        primary_region={'code': 'GLOBAL', 'name': '全球'}, related_regions=[], assets=[], uncertainties=[], scenarios=[])


def word(canonical, alias, kind='entity'):
    return {'canonical': canonical, 'aliases': [alias], 'kind': kind, 'quote': alias}


def test_large_lexicon_reproducible_and_no_ticker_company_conflation():
    from importlib.util import module_from_spec, spec_from_file_location
    root = Path(__file__).resolve().parents[2]
    spec = spec_from_file_location('lexicon_builder', root / 'scripts/build_briefing_lexicon.py')
    builder = module_from_spec(spec)
    spec.loader.exec_module(builder)
    generated = builder.build()
    assert generated == json.loads(keywords.DATA.read_text())
    assert len(generated['entries']) > 8000
    assert keywords.lexicon().resolve('AAPL') is None
    assert keywords.lexicon().resolve('Apple Inc.')['canonical'] == '苹果公司'


def test_source_snapshot_tampering_fails(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    root = Path(__file__).resolve().parents[2]
    spec = spec_from_file_location('lexicon_builder', root / 'scripts/build_briefing_lexicon.py')
    builder = module_from_spec(spec)
    spec.loader.exec_module(builder)
    (tmp_path / 'sources').mkdir()
    (tmp_path / 'sources/manifest.json').write_text(json.dumps({'sources': {'test.json': {'sha256': 'wrong'}}}))
    (tmp_path / 'sources/test.json').write_text('{}')
    with pytest.raises(ValueError, match='词表来源摘要不匹配'):
        builder.build(tmp_path)


def test_divisions_and_longest_names_stay_distinct():
    article = {'content_text': 'Goldman Sachs Research、Goldman Sachs Executive Office、Goldman Sachs。HBM4e和HBM；EURO STOXX 50与EURO STOXX。'}
    actual = [item['canonical'] for item in keywords.candidates(article)['items']]
    assert actual == ['高盛研究部', '高盛执行办公室', '高盛集团', 'HBM4e', 'HBM', 'EURO STOXX 50指数', 'EURO STOXX指数']
    assert '高盛集团' not in keywords.query_names('高盛研究')
    assert '高盛研究部' in keywords.query_names('Goldman Sachs Research')


def test_word_boundaries_and_ambiguous_names_do_not_create_candidates():
    actual = keywords.candidates({'content_text': 'Apple apple Meta Amazon Turkey Georgia Chase SEC ETHUSDT wstETH HBM4ever'})
    assert actual['items'] == []
    assert keywords.query_names('Georgia') is None


def test_currency_names_do_not_match_chinese_compounds_or_euro_area():
    actual = keywords.candidates({'content_text': '银行资金。euro area与Euro-zone和欧元区。'})
    assert [(item['canonical'], item['kind']) for item in actual['items']] == [('欧元区', 'country')]
    assert keywords.query_names('银') is not None
    assert keywords.candidates({'content_text': '欧元走弱，euro depreciates。'})['items'][0]['canonical'] == '欧元'


def test_candidate_limit_is_explicit_and_input_untouched():
    names = [entry['canonical'] for entry in keywords.lexicon().entries.values() if entry['kind'] == 'country'][:100]
    article = {'title': '全部国家', 'content_text': '、'.join(names)}
    before = copy.deepcopy(article)
    hints = keywords.candidates(article)
    assert hints['matched_count'] >= 90
    assert len(hints['items']) == keywords.MAX_HINTS
    assert hints['truncated'] is True
    assert article == before


def test_normalizes_and_merges_names_without_inventing_literal_aliases():
    body = '高盛研究和Goldman Sachs Research讨论美元。'
    raw = result_for(body, [word('高盛研究', '高盛研究'), word('高盛研究部', 'Goldman Sachs Research')])
    before = raw.model_dump()
    output, audit = keywords.normalize(raw, {'content_text': body})
    research = output.keywords[0]
    assert research.canonical == '高盛研究部'
    assert research.aliases == ['高盛研究', 'Goldman Sachs Research']
    assert '高盛研究部' not in research.aliases
    assert raw.model_dump() == before
    assert audit['raw_keywords'] == before['keywords']
    assert audit['sha256'] == keywords.lexicon().digest
    contracts.validate_evidence(output, {'content_text': body})


@pytest.mark.parametrize('canonical,alias,kind', [
    ('比特币ETF', 'BTC', 'asset'), ('英伟达股票', 'NVIDIA', 'asset'),
    ('NVIDIA', 'NVIDIA', 'asset'), ('高盛研究部', 'Goldman Sachs', 'entity'),
    ('Georgia', 'Georgia', 'entity'),
])
def test_unknown_products_and_conflicting_roles_not_rewritten(canonical, alias, kind):
    raw = result_for(alias, [word(canonical, alias, kind)])
    output, _ = keywords.normalize(raw, {'content_text': alias})
    assert output.keywords[0].model_dump() == raw.keywords[0].model_dump()


@pytest.mark.parametrize('name,canonical,base,base_canonical', [
    ('Ethereum Classic', '以太坊经典', 'Ethereum', '以太坊'),
    ('Bitcoin Cash', '比特币现金', 'Bitcoin', '比特币'),
    ('比特币现金', '比特币现金', '比特币', '比特币'),
])
def test_complete_asset_alias_blocks_inner_match_but_not_separate_occurrence(name, canonical, base, base_canonical):
    body = f'{name}完成升级。'
    words = [word(canonical, name, 'asset')]
    output, audit = keywords.normalize(result_for(body, words), {'content_text': body})
    assert [item.canonical for item in output.keywords] == [canonical]
    assert audit['added'] == []

    body += f'{base}另有独立变化。'
    raw = result_for(body, words)
    before = raw.model_dump()
    output, audit = keywords.normalize(raw, {'content_text': body})
    assert [item.canonical for item in output.keywords] == [canonical, base_canonical]
    assert len(audit['added']) == 1
    assert raw.model_dump() == before
    contracts.validate_evidence(output, {'content_text': body})


def test_fact_evidence_restores_currencies_and_technology_but_not_background():
    fact = '研究指出欧元走弱会改变美元收入折算，HBM4e升级仍有风险。'
    body = fact + '背景介绍提到日本和英伟达。'
    raw = result_for(body, quote=fact)
    output, audit = keywords.normalize(raw, {'content_text': body})
    assert {item.canonical for item in output.keywords} == {'研究', '欧元', '美元', 'HBM4e'}
    assert {item['id'] for item in audit['added']} == {'cldr:currencies:EUR', 'cldr:currencies:USD', 'local:hbm4e'}
    assert {item['canonical'] for item in audit['unselected_candidates']} == {'日本', '英伟达'}
    assert output.importance_score == raw.importance_score


def test_invalid_evidence_rejected_before_normalizing():
    raw = result_for('研究', [word('高盛研究部', 'Goldman Sachs Research')])
    with pytest.raises(ValueError):
        keywords.normalize(raw, {'content_text': '研究'})


def test_filters_invalid_alias_without_mutating_raw_result():
    body = 'Consumer spending in the U.S. rose; U.S. consumers saved less.'
    raw = result_for(body, [{'canonical': '美国消费者支出', 'aliases': [
        'Consumer spending in the U.S.', 'U.S. consumer spending'],
        'kind': 'topic', 'quote': 'Consumer spending in the U.S.'}])
    before = raw.model_dump()
    output, audit = keywords.normalize(raw, {'content_text': body})
    assert output.keywords[0].aliases == ['Consumer spending in the U.S.']
    assert audit['raw_keywords'] == before['keywords']
    assert audit['processing_version'] == 'keywords:v3'
    assert audit['decisions'] == [{'action': 'drop_invalid_alias', 'canonical': '美国消费者支出',
                                    'alias': 'U.S. consumer spending', 'reason': 'not_literal_in_single_field'}]
    assert raw.model_dump() == before
    contracts.validate_evidence(output, {'content_text': body})


def test_all_invalid_aliases_fail_explicitly():
    raw = result_for('研究', [{'canonical': '研究', 'aliases': ['RESEARCH'], 'kind': 'topic', 'quote': '研究'}])
    before = raw.model_dump()
    with pytest.raises(ValueError, match='关键词无可保留的原文别名'):
        keywords.normalize(raw, {'content_text': '研究'})
    assert raw.model_dump() == before


@pytest.mark.parametrize('article,alias', [
    ({'title': 'Consumer', 'summary': 'spending increased'}, 'Consumer spending'),
    ({'title': 'Consumer spending'}, 'consumer'),
    ({'title': 'Consumer spending'}, 'Consumer  spending'),
])
def test_alias_filter_requires_exact_contiguous_field(article, alias):
    raw = result_for('Consumer', [{'canonical': '消费', 'aliases': ['Consumer', alias],
                                   'kind': 'topic', 'quote': 'Consumer'}])
    output, audit = keywords.normalize(raw, article)
    assert output.keywords[0].aliases == ['Consumer']
    assert audit['decisions'][0]['alias'] == alias


@pytest.mark.parametrize('fact_quote,keyword_quote,error', [
    ('不存在的事实', 'Consumer', '事实证据'),
    ('Consumer', '不存在的关键词', '关键词证据'),
])
def test_invalid_quotes_still_fail_after_alias_filter(fact_quote, keyword_quote, error):
    raw = result_for('Consumer', [{'canonical': '消费', 'aliases': ['Consumer', 'consumer'],
                                   'kind': 'topic', 'quote': keyword_quote}], quote=fact_quote)
    with pytest.raises(ValueError, match=error):
        keywords.normalize(raw, {'content_text': 'Consumer'})


def test_role_conflict_returns_filtered_not_raw_result():
    body = '高盛研究和Goldman Sachs Research讨论市场。'
    raw = result_for(body, [word('高盛研究', '高盛研究'), {
        'canonical': '高盛研究部', 'aliases': ['Goldman Sachs Research', 'Goldman  Sachs Research'],
        'kind': 'asset', 'quote': 'Goldman Sachs Research'}])
    before = raw.model_dump()
    output, audit = keywords.normalize(raw, {'content_text': body})
    assert audit['conflict'] == '高盛研究部'
    assert audit['raw_keywords'] == before['keywords']
    assert audit['decisions'][0]['action'] == 'drop_invalid_alias'
    assert output.keywords[1].aliases == ['Goldman Sachs Research']
    assert raw.model_dump() == before
    contracts.validate_evidence(output, {'content_text': body})


def test_collision_does_not_select_arbitrary_entity():
    dictionary = keywords.Lexicon({'version': 'test', 'entries': [
        {'id': 'one', 'canonical': '甲', 'kind': 'entity', 'aliases': ['shared']},
        {'id': 'two', 'canonical': '乙', 'kind': 'entity', 'aliases': ['shared']},
    ]}, 'test')
    assert dictionary.resolve('shared') is None
    assert list(dictionary.matches('shared')) == []


@pytest.mark.parametrize('stage', ['analysis', 'period', 'daily'])
def test_runtime_policy_applies_to_old_and_candidate_skills_once(app_env, stage):
    from alphapulse.briefings.config import system_prompt
    old = '既有发布内容'
    assert old in system_prompt(old, stage)
    for text in [old, contracts.SKILL_TEMPLATES[stage][1]]:
        prompt = system_prompt(text, stage)
        assert prompt.count('importance:publication:v1') == 1
        assert prompt.count('关键词规范与覆盖检查（keywords:v1）') == (1 if stage == 'analysis' else 0)
