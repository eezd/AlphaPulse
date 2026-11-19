"""离线评估的来源绑定、契约检查和人工评审边界；全部使用合成文件。"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


@pytest.fixture
def quality():
    return SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/briefing_quality.py')))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))
    return sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def dataset(tmp_path, quality):
    model = {'provider_id': 1, 'model': 'synthetic', 'max_tokens': 8192,
             'context_window': 250000, 'reasoning_effort': 'xhigh'}
    article = {'title': 'ETH上涨5%', 'summary': '', 'content_text': '', 'revision_no': 2,
               'content_hash': 'frozen-source', 'published_at': '2026-11-01T00:00:00Z',
               'first_seen_at': '2026-11-02T00:00:00Z'}
    output = {'keywords': [{'canonical': '以太坊', 'aliases': ['ETH'], 'kind': 'asset', 'quote': 'ETH'}],
              'summary': '来源报道ETH上涨5%', 'facts': [{'text': '来源报道ETH上涨5%', 'quote': 'ETH上涨5%'}],
              'impact': '未提供原因', 'importance_score': 35, 'importance_reason': '局部价格变化',
              'primary_region': {'code': 'CRYPTO', 'name': '加密货币'}, 'related_regions': [],
              'assets': [], 'uncertainties': ['时点未给'], 'scenarios': []}
    cases, labels = [], []
    for category, count in quality.QUOTAS.items():
        for _ in range(count):
            case_id = len(cases) + 1
            done = category in ('flash', 'crypto', 'official', 'digest')
            row = {'id': case_id, 'category': category, 'input': deepcopy(article),
                   'input_sha256': quality.fingerprint(article), 'rules': deepcopy(model),
                   'rules_sha256': quality.fingerprint(model), 'source_revision': 2,
                   'content_hash': 'frozen-source', 'source_capture': {'revision_no': 2,
                   'content_hash': 'frozen-source', 'captured_at': '2026-11-02T01:00:00Z'},
                   'effective_at': article['published_at'], 'time_basis': 'published_at',
                   'state': 'done' if done else 'failed', 'output': deepcopy(output) if done else None,
                   'usage': None if category == 'oversize' else {'input_tokens': 10, 'output_tokens': 5}}
            cases.append(row)
            labels.append({'id': case_id, 'scope': 'admission' if category == 'oversize' else 'semantic',
                'time_note': '发布时间、版本取得时间不同', 'critical_errors': ['不得把首次入库日作事件日'],
                'time_anchors': {'published_at': article['published_at'], 'first_seen_at': article['first_seen_at'],
                    'source_captured_at': row['source_capture']['captured_at'],
                    'effective_at': row['effective_at'], 'time_basis': row['time_basis']},
                'checks': [{'id': f'{case_id}:01', 'aspect': 'number', 'claim': '来源报道上涨5%',
                            'evidence': {'field': 'title', 'quote': 'ETH上涨5%'}}]})
    checksum = write_json(tmp_path / 'baseline.json', {'format_version': 1, 'cases': cases})
    (tmp_path / 'baseline.sha256').write_text(checksum)
    gold_digest = write_json(tmp_path / 'gold.json', {'format_version': 1, 'baseline_sha256': checksum, 'cases': labels})
    candidate = {'baseline_sha256': checksum, 'model_config': model, 'skill_sha256': 'a' * 64,
                 'cases': [{**deepcopy(row), 'not_sent': row['category'] == 'oversize'} for row in cases]}
    return SimpleNamespace(path=tmp_path, checksum=checksum, gold_digest=gold_digest,
                           cases=cases, labels=labels, candidate=candidate)


def test_baseline_separates_contract_success_from_unreviewed_semantics(quality, dataset):
    report = quality.evaluate(dataset.path)
    run = report['runs']['baseline']
    assert report['cases'] == 100 and report['semantic_cases'] == 96
    assert run['contract_valid_done'] == 60 and run['contract_errors'] == []
    assert run['not_sent'] == 4 and run['usage']['known_calls'] == 96
    assert run['usage']['unknown_calls'] == 0 and run['usage']['input_tokens'] == 960
    assert run['semantic']['reviewed_cases'] == 0 and run['semantic']['complete'] is False
    assert len(run['semantic']['pending_ids']) == 60


@pytest.mark.parametrize('mutation', ['file', 'input', 'revision', 'quota', 'gold_quote', 'time'])
def test_rejects_frozen_input_or_annotation_drift(quality, dataset, mutation):
    if mutation in ('gold_quote', 'time'):
        gold = json.loads((dataset.path / 'gold.json').read_text())
        if mutation == 'gold_quote':
            gold['cases'][0]['checks'][0]['evidence']['quote'] = '虚构文本'
        else:
            gold['cases'][0]['time_anchors']['source_captured_at'] = '2020-01-01'
        write_json(dataset.path / 'gold.json', gold)
    else:
        baseline = json.loads((dataset.path / 'baseline.json').read_text())
        row = baseline['cases'][0]
        if mutation in ('file', 'input'):
            row['input']['title'] = 'ETH下跌'
        elif mutation == 'revision':
            row['source_capture']['revision_no'] = 1
        else:
            row['category'] = 'official'
        digest = write_json(dataset.path / 'baseline.json', baseline)
        if mutation != 'file':
            (dataset.path / 'baseline.sha256').write_text(digest)
    with pytest.raises(ValueError):
        quality.load_baseline(dataset.path)


@pytest.mark.parametrize('mutation', ['parameter', 'missing', 'duplicate', 'input'])
def test_candidates_must_keep_parameters_and_all_frozen_inputs(quality, dataset, mutation):
    candidate = dataset.candidate
    if mutation == 'parameter':
        candidate['model_config']['max_tokens'] += 1
    elif mutation == 'missing':
        candidate['cases'].pop()
    elif mutation == 'duplicate':
        candidate['cases'].append(candidate['cases'][0])
    else:
        candidate['cases'][0]['input_sha256'] = 'b' * 64
    path = dataset.path / 'candidate.json'
    write_json(path, candidate)
    with pytest.raises(ValueError):
        quality.evaluate(dataset.path, path)


def test_candidate_diagnostics_do_not_accept_invented_aliases_or_enum(quality, dataset):
    rows = dataset.candidate['cases']
    rows[0]['output']['keywords'][0]['aliases'] = ['以太坊']
    rows[1]['output']['keywords'][0]['kind'] = 'index'
    rows[2]['output']['facts'][0]['quote'] = '上涨6%'
    rows[-1]['not_sent'] = False
    path = dataset.path / 'candidate.json'
    digest = write_json(path, dataset.candidate)
    report = quality.evaluate(dataset.path, path)
    run = report['runs']['candidate']
    assert report['candidate_sha256'] == digest and report['candidate_skill_sha256'] == 'a' * 64
    assert run['contract_valid_done'] == 57 and len(run['contract_errors']) == 3
    assert run['admission_violation_ids'] == [100]
    assert run['semantic']['complete'] is False


def test_semantic_review_binds_gold_and_output_and_preserves_uncertainty(quality, dataset):
    path = dataset.path / 'candidate.json'
    digest = write_json(path, dataset.candidate)
    reviews = {'baseline_sha256': dataset.checksum, 'gold_sha256': dataset.gold_digest,
               'candidate_sha256': digest, 'candidate': [{'id': 1, 'checks': {'1:01': 'uncertain'},
                     'critical_error': False, 'notes': '时点不足，不能确定'}]}
    review_path = dataset.path / 'reviews.json'
    write_json(review_path, reviews)
    result = quality.evaluate(dataset.path, path, review_path)['runs']['candidate']['semantic']
    assert result['reviewed_cases'] == 1 and result['checks']['uncertain'] == 1 and result['complete'] is False
    reviews['gold_sha256'] = 'changed'
    write_json(review_path, reviews)
    with pytest.raises(ValueError, match='标注或结果摘要'):
        quality.evaluate(dataset.path, path, review_path)


def test_all_failed_is_not_complete_and_cannot_hide_semantic_reviews(quality, dataset):
    cases = quality.indexed(dataset.cases)
    labels = quality.indexed(dataset.labels)
    failed = {i: {**r, 'state': 'failed', 'output': None} for i, r in cases.items()}
    assert quality.semantic_summary(cases, labels, failed, {})['complete'] is False
    with pytest.raises(ValueError, match='无成功输出'):
        quality.semantic_summary(cases, labels, failed, {1: {'id': 1}})
    with pytest.raises(ValueError, match='只评准入'):
        quality.semantic_summary(cases, labels, failed, {100: {'id': 100}})


@pytest.mark.parametrize('seconds', [float('nan'), float('inf'), -1, True])
def test_elapsed_must_be_finite_nonnegative_number(quality, dataset, seconds):
    records = quality.indexed(dataset.candidate['cases'])
    records[1]['elapsed_seconds'] = seconds
    with pytest.raises(ValueError, match='耗时无效'):
        quality.summarize(quality.indexed(dataset.cases), records)
