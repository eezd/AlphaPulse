#!/usr/bin/env python3
"""离线核验单篇分析固定样本；只读取本地文件，不连接模型、数据库或Redis。"""
import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError
from alphapulse.briefings.contracts import NewsAnalysisResult, validate_evidence
from alphapulse.briefings.queries import usage_totals

QUOTAS = {'flash': 20, 'crypto': 10, 'official': 20, 'digest': 10,
          'fact_evidence': 10, 'keyword_evidence': 10, 'alias_evidence': 10, 'schema': 6, 'oversize': 4}
MODEL_KEYS = ('provider_id', 'model', 'max_tokens', 'context_window', 'reasoning_effort')


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def indexed(records):
    result = {item['id']: item for item in records}
    require(len(result) == len(records), '样本ID重复')
    return result


def load_baseline(directory):
    raw = (directory / 'baseline.json').read_bytes()
    checksum = sha256(raw).hexdigest()
    require(checksum == (directory / 'baseline.sha256').read_text().strip(), '冻结基线文件摘要不一致')
    baseline = json.loads(raw)
    require(baseline['format_version'] == 1, '未知基线格式')
    cases = indexed(baseline['cases'])
    require(Counter(row['category'] for row in cases.values()) == QUOTAS, '样本分层数量不符合固定100条配额')
    configuration = {key: next(iter(cases.values()))['rules'][key] for key in MODEL_KEYS}
    for row in cases.values():
        require(fingerprint(row['input']) == row['input_sha256'], f'{row["id"]}: 原文摘要不一致')
        require(fingerprint(row['rules']) == row['rules_sha256'], f'{row["id"]}: 冻结规则摘要不一致')
        require({key: row['rules'][key] for key in MODEL_KEYS} == configuration, '基线模型参数不一致')
        capture = row['source_capture']
        require(capture is not None and capture['content_hash'] == row['content_hash']
                and capture['revision_no'] == row['source_revision']
                and row['input']['content_hash'] == row['content_hash']
                and row['input']['revision_no'] == row['source_revision'], f'{row["id"]}: 来源修订不一致')
    gold = json.loads((directory / 'gold.json').read_text())
    require(gold['format_version'] == 1 and gold['baseline_sha256'] == checksum, '标注与冻结基线不匹配')
    labels = indexed(gold['cases'])
    require(labels.keys() == cases.keys(), '标注必须覆盖全部100条，不能多项或缺项')
    for case_id, label in labels.items():
        row = cases[case_id]
        require(label['scope'] == ('admission' if row['category'] == 'oversize' else 'semantic'), f'{case_id}: 标注范围错误')
        require(label['time_note'] and label['critical_errors'], f'{case_id}: 缺少时间边界或重大错误检查点')
        anchors = {'published_at': row['input']['published_at'], 'first_seen_at': row['input']['first_seen_at'],
                   'source_captured_at': row['source_capture']['captured_at'],
                   'effective_at': row['effective_at'], 'time_basis': row['time_basis']}
        require(label['time_anchors'] == anchors, f'{case_id}: 时间锚点与冻结来源不一致')
        checks = label['checks']
        indexed(checks)
        require(bool(checks), f'{case_id}: 缺少原文检查项')
        for check in checks:
            require(check['claim'] and check['aspect'] in ('fact', 'number', 'status', 'entity', 'time', 'scope'),
                    f'{case_id}: 检查项类型或内容无效')
            evidence = check['evidence']
            require(evidence['field'] in ('title', 'summary', 'content_text'), f'{case_id}: 引文字段无效')
            quote = evidence['quote']
            require(quote.strip() and quote in (row['input'].get(evidence['field']) or ''), f'{case_id}: 标注引文不在冻结原文中')
    return checksum, cases, labels, configuration


def summarize(cases, records):
    require(records.keys() == cases.keys(), '结果必须与冻结样本逐项对应')
    states = Counter()
    diagnostics = []
    known = []
    elapsed = []
    by_category = {}
    admission_violations = []
    for category in QUOTAS:
        members = [row for case_id, row in records.items() if cases[case_id]['category'] == category]
        by_category[category] = dict(Counter(row['state'] for row in members))
    for case_id, row in records.items():
        if cases[case_id]['category'] == 'oversize' and not row.get('not_sent', False):
            admission_violations.append(case_id)
    for case_id, row in records.items():
        state = row['state']
        require(state in ('done', 'failed', 'blocked'), f'{case_id}: 结果状态无效')
        states[state] += 1
        if state == 'done':
            try:
                result = NewsAnalysisResult.model_validate(row['output'])
                validate_evidence(result, cases[case_id]['input'])
            except ValidationError as error:
                diagnostics.append({'id': case_id, 'error': 'schema',
                                    'locations': ['.'.join(map(str, item['loc'])) for item in error.errors()]})
            except ValueError as error:
                diagnostics.append({'id': case_id, 'error': str(error)})
        else:
            require(row.get('output') is None, f'{case_id}: 失败/阻塞结果不能携带成功输出')
        # 发送前超限明确没有调用，不能把null usage误算成未知计费调用。
        no_request = row.get('not_sent', False)
        require(type(no_request) is bool, f'{case_id}: not_sent必须为布尔值')
        require(not no_request or (state != 'done' and row.get('usage') is None), f'{case_id}: 未发送标志与输出/用量矛盾')
        if not no_request:
            known.append(SimpleNamespace(reused_from=None, started_at=True, usage=row.get('usage')))
        if row.get('elapsed_seconds') is not None:
            seconds = row['elapsed_seconds']
            require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0, f'{case_id}: 耗时无效')
            elapsed.append(seconds)
    return {'states': dict(states), 'by_category': by_category,
            'admission_violation_ids': admission_violations,
            'contract_valid_done': states['done'] - len(diagnostics),
            'contract_errors': diagnostics, 'usage': usage_totals(known),
            'not_sent': sum(row.get('not_sent', False) for row in records.values()),
            'elapsed_seconds': sum(elapsed) if len(elapsed) == len(records) else None,
            'elapsed_known_cases': len(elapsed)}


def semantic_summary(cases, labels, records, reviews):
    require(not (reviews.keys() - cases.keys()), '语义评审包含未知ID')
    coverage = Counter()
    critical = []
    pending = []
    reviewed = 0
    for case_id, case in cases.items():
        if case['category'] == 'oversize':
            require(case_id not in reviews, f'{case_id}: 超长样本只评准入，不能填写语义评审')
            continue
        checks = {check['id'] for check in labels[case_id]['checks']}
        review = reviews.get(case_id)
        if records[case_id]['state'] != 'done':
            require(review is None, f'{case_id}: 无成功输出不能填写语义评审')
            coverage['unavailable'] += len(checks)
            continue
        if review is None:
            pending.append(case_id)
            coverage['unreviewed'] += len(checks)
            continue
        reviewed += 1
        require(review['checks'].keys() == checks, f'{case_id}: 语义评审须覆盖该条全部检查项')
        require(type(review['critical_error']) is bool and bool(review['notes'].strip()), f'{case_id}: 评审须给出重大错误结论及理由')
        for verdict in review['checks'].values():
            require(verdict in ('pass', 'fail', 'uncertain'), f'{case_id}: 语义评审结论无效')
            coverage[verdict] += 1
        if review['critical_error']:
            critical.append(case_id)
    return {'checks': dict(coverage), 'critical_error_ids': critical,
            'reviewed_cases': reviewed, 'pending_ids': pending,
            'complete': reviewed > 0 and not pending and not coverage['uncertain']}


def evaluate(directory, candidate_path=None, review_path=None):
    checksum, cases, labels, configuration = load_baseline(directory)
    baseline = {case_id: {**row, 'not_sent': row['category'] == 'oversize'} for case_id, row in cases.items()}
    runs = {'baseline': baseline}
    gold_digest = sha256((directory / 'gold.json').read_bytes()).hexdigest()
    skill_digest = None
    candidate_digest = None
    if candidate_path is not None:
        raw = candidate_path.read_bytes()
        candidate_digest = sha256(raw).hexdigest()
        candidate = json.loads(raw)
        require(candidate['baseline_sha256'] == checksum and candidate['model_config'] == configuration,
                '候选结果改变了冻结样本或模型参数')
        require(len(candidate['skill_sha256']) == 64 and all(c in '0123456789abcdef' for c in candidate['skill_sha256']),
                '候选结果缺少Skill文件SHA256')
        skill_digest = candidate['skill_sha256']
        runs['candidate'] = indexed(candidate['cases'])
        for row in runs['candidate'].values():
            require(row['id'] in cases and row.get('input_sha256') == cases[row['id']]['input_sha256'],
                    f'{row["id"]}: 候选结果未绑定冻结输入摘要')
    reviews = {}
    if review_path is not None:
        reviews = json.loads(review_path.read_text())
        require(reviews['baseline_sha256'] == checksum and reviews['candidate_sha256'] == candidate_digest
                and reviews['gold_sha256'] == gold_digest, '语义评审文件与本次输入、标注或结果摘要不一致')
        require(not (reviews.keys() - {'baseline_sha256', 'gold_sha256', 'candidate_sha256', 'baseline', 'candidate'}), '未知评审字段')
        require(candidate_path is not None or not reviews.get('candidate'), '没有候选结果不能评审候选')
    report = {'cases': len(cases), 'semantic_cases': len(cases) - QUOTAS['oversize'],
              'baseline_sha256': checksum, 'gold_sha256': gold_digest, 'model_config': configuration,
              'candidate_sha256': candidate_digest, 'candidate_skill_sha256': skill_digest, 'runs': {}}
    for name, records in runs.items():
        report['runs'][name] = {**summarize(cases, records),
            'semantic': semantic_summary(cases, labels, records, indexed(reviews.get(name, [])))}
    report['quality_verdict'] = '未做模型回跑；尚不能判断v3改进效果' if candidate_path is None else '需结合逐条语义评审，结构校验通过不代表事实准确'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='私有冻结样本目录')
    parser.add_argument('--candidate', type=Path, help='已有离线候选结果；本工具不发送请求')
    parser.add_argument('--reviews', type=Path, help='绑定结果摘要的逐条语义评审')
    args = parser.parse_args()
    print(json.dumps(evaluate(args.directory, args.candidate, args.reviews), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
