"""单篇分析持久化执行：在任何调用前冻结计划，未知在途请求不重放。"""
from datetime import UTC, datetime
from sqlalchemy import select
from alphapulse.ai import progress, submission
from alphapulse.briefings import analysis_model, analysis_parts, contracts
from alphapulse.briefings.models import Analysis, AnalysisPart


class OwnershipLost(Exception):
    pass


async def run(session, row, revision, provider, system, article, rules, owns):
    reusable_rules = {key: value for key, value in rules.items() if key != 'frozen_from'}
    frozen_rules = {'version': 'analysis:parts:v1', 'rules_hash': analysis_parts.fingerprint(reusable_rules),
                    'provider': analysis_parts.fingerprint((provider.id, provider.protocol, provider.base_url,
                                                             provider.model, provider.reasoning_effort, provider.max_tokens,
                                                             provider.context_window))}
    repair_part = None
    repair_before = None

    async def start_repair(repair_system, payload, schema):
        nonlocal repair_part, repair_before
        if not await owns(session, row, revision):
            raise OwnershipLost()
        repair_before = len(budget.usages)
        first = budget.active_part
        first.output = budget.last_raw
        first.usage = {'rounds': list(budget.usages[budget.active_start:repair_before])}
        first.state, first.error = 'waiting_repair', payload['error']
        row.usage = budget.usage
        repair_part = AnalysisPart(analysis_id=row.id, stage='repair', part_index=0, state='processing',
            input_hash=analysis_parts.fingerprint((frozen_rules, repair_system, schema.model_json_schema(), payload)),
            input=payload, rules=frozen_rules, control_revision=revision, started_at=datetime.now(UTC))
        session.add(repair_part)
        await session.commit()

    async def end_repair(raw, usage, error):
        if not await owns(session, row, revision):
            raise OwnershipLost()
        repair_part.output, repair_part.usage = raw, usage
        repair_part.state = 'failed' if error else 'done'
        repair_part.error = str(error) if error else None
        repair_part.finished_at = datetime.now(UTC)
        row.usage = budget.usage
        await session.commit()

    budget = analysis_model.Budget(provider, on_repair_start=start_repair, on_repair_end=end_repair)
    if submission.fits_context(provider, system, article, contracts.NewsAnalysisDraft):
        return await _short(session, row, revision, provider, system, article, frozen_rules, budget, owns, lambda: repair_before)
    return await _long(session, row, revision, provider, system, article, frozen_rules, budget, owns, lambda: repair_before)


async def execute_part(session, row, part, revision, budget, system, schema, validator, owns, repair_before,
                       *, evidence_corrections=None):
    if not await owns(session, row, revision):
        raise OwnershipLost()
    part.state, part.started_at = 'processing', datetime.now(UTC)
    await session.commit()
    before = len(budget.usages)
    budget.active_part, budget.active_start = part, before
    try:
        result = await budget.submit(system, part.input, schema, validator)
    except OwnershipLost:
        raise
    except Exception as error:
        if await owns(session, row, revision):
            part.output, part.state, part.error = budget.last_raw, 'failed', str(error)
            own_end = repair_before()
            end = own_end if own_end is not None and own_end >= before else len(budget.usages)
            part.usage = {'rounds': budget.usages[before:end]} if end > before else None
            part.finished_at, row.usage = datetime.now(UTC), budget.usage
            await session.commit()
        raise
    if not await owns(session, row, revision):
        raise OwnershipLost()
    value = result[0] if isinstance(result, tuple) else result
    part.output = value.model_dump(exclude_none=isinstance(value, analysis_parts.MapResult))
    if evidence_corrections:
        part.evidence_corrections = list(evidence_corrections)
    own_end = repair_before()
    end = own_end if own_end is not None and own_end >= before else len(budget.usages)
    part.usage = {'rounds': budget.usages[before:end]} if end > before else None
    part.state, part.error, part.finished_at = 'done', None, datetime.now(UTC)
    row.usage = budget.usage
    await session.commit()
    return result


async def _short(session, row, revision, provider, system, article, rules, budget, owns, repair_before):
    part = AnalysisPart(analysis_id=row.id, stage='final', part_index=0, state='pending',
        input_hash=analysis_parts.fingerprint((rules, system, contracts.NewsAnalysisDraft.model_json_schema(), article)),
        input=article, rules=rules, control_revision=revision)
    session.add(part)
    await session.commit()
    with progress.model_request(provider):
        result = await execute_part(session, row, part, revision, budget, system, contracts.NewsAnalysisDraft,
                                    lambda draft: analysis_model.normalize_draft(draft, article), owns, repair_before)
    return (*result, budget.usage)


async def _long(session, row, revision, provider, system, article, rules, budget, owns, repair_before):
    planned, limit = analysis_parts.plan(article, provider, system)
    if not planned:
        raise ValueError('完整新闻无可分段的正文；未发送模型请求')
    row.rules = {**row.rules, 'analysis_plan': {'version': 'analysis:parts:v1', 'map_count': len(planned),
                                                'map_output_limit': limit, 'overlap': analysis_parts.OVERLAP}}
    mapped_system = analysis_parts.map_system(system)
    parts = []
    for index, (left, right, payload) in enumerate(planned):
        part = AnalysisPart(analysis_id=row.id, stage='map', part_index=index, start_offset=left, end_offset=right,
            state='pending', input_hash=analysis_parts.fingerprint((rules, mapped_system,
                analysis_parts.MapResult.model_json_schema(), payload, limit)),
            input=payload, rules={**rules, 'map_output_limit': limit}, control_revision=revision)
        parts.append(part)
        session.add(part)
    final = AnalysisPart(analysis_id=row.id, stage='final', part_index=0, state='pending',
        input_hash=analysis_parts.fingerprint((rules, 'final', len(planned))),
        input={'planned_map_count': len(planned)}, rules=rules, control_revision=revision)
    session.add(final)
    await session.commit()
    mapped = []
    for index, part in enumerate(parts):
        progress.step(f'分析全文片段 {index + 1}/{len(parts)}', operation='单篇分析')
        reused = None
        if (row.rules or {}).get('frozen_from'):
            reused = await session.scalar(select(AnalysisPart).join(Analysis, AnalysisPart.analysis_id == Analysis.id)
                .where(Analysis.news_id == row.news_id, AnalysisPart.analysis_id != row.id,
                       AnalysisPart.stage == 'map', AnalysisPart.part_index == index,
                       AnalysisPart.input_hash == part.input_hash, AnalysisPart.state == 'done')
                .order_by(AnalysisPart.id.desc()).limit(1))
        if reused is not None and reused.rules == part.rules and reused.input == part.input:
            checked = analysis_parts.validate_map(analysis_parts.MapResult.model_validate(reused.output),
                                                  article, part.start_offset, part.end_offset, limit)
            part.state, part.output, part.reused_from = 'done', checked.model_dump(exclude_none=True), reused.id
            part.evidence_corrections = reused.evidence_corrections
            part.finished_at = datetime.now(UTC)
            await session.commit()
            mapped.append(checked)
            continue
        corrections = []

        def validate(output, p=part, audit=corrections):
            audit.clear()
            return analysis_parts.validate_map(output, article, p.start_offset, p.end_offset, limit,
                                               corrections=audit)

        with progress.model_request(provider):
            item = await execute_part(session, row, part, revision, budget, mapped_system, analysis_parts.MapResult,
                validate, owns, repair_before, evidence_corrections=corrections)
        mapped.append(item)
    contents = [analysis_parts.map_json(item) for item in mapped]
    required = [anchor for _, _, payload in planned for anchor in payload['material_anchors']]
    final.input = analysis_parts.final_payload(article, contents, required)
    final.input_hash = analysis_parts.fingerprint((rules, analysis_parts.final_system(system),
                                                    contracts.NewsAnalysisDraft.model_json_schema(), final.input))
    if not analysis_parts.fits_reserved(provider, analysis_parts.final_system(system), final.input, contracts.NewsAnalysisDraft):
        raise ValueError('分段汇总超出模型上下文；未发送最终模型请求')
    await session.commit()
    progress.step('汇总全文证据', operation='单篇分析')
    with progress.model_request(provider):
        result = await execute_part(session, row, final, revision, budget, analysis_parts.final_system(system),
            contracts.NewsAnalysisDraft, lambda draft: analysis_parts.validate_final(draft, article, mapped),
            owns, repair_before)
    return (*result, budget.usage)
