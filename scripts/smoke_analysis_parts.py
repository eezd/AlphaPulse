"""独占临时数据库中的长文分段、重启恢复与显式冻结重试验收。"""
import asyncio
import os
from unittest.mock import patch

if os.environ.get('BRIEFINGS_TEST_ISOLATED') != '1':
    raise RuntimeError('必须指定 BRIEFINGS_TEST_ISOLATED=1；禁止使用生产资源')
for name in ('DATABASE_URL', 'REDIS_URL'):
    os.environ[name] = os.environ['BRIEFINGS_TEST_' + name]
os.environ.update(JWT_SECRET='analysis-parts-test-only-secret-32-characters', ADMIN_USERNAME='analysis-parts-test',
                  SECRET_ENCRYPTION_KEY='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import aliased
from alphapulse.ai import submission
from alphapulse.briefings import analysis_execution, analysis_parts, config, contracts, service
from alphapulse.briefings.models import Analysis, AnalysisPart
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem


def evidence():
    return {'field': 'title', 'start': 0, 'end': 3, 'quote': 'BTC'}


def map_result():
    return analysis_parts.MapResult(summary='标题', facts=[{'text': 'BTC报道', 'evidence': evidence()}],
        keywords=[{'canonical': 'BTC', 'kind': 'asset', 'quote': evidence(), 'aliases': [evidence()]}])


def final_result():
    return contracts.NewsAnalysisDraft(keywords=[{'canonical': 'BTC', 'aliases': ['BTC'], 'kind': 'asset', 'quote': 'BTC'}],
        summary='报道摘要', facts=[{'text': 'BTC报道', 'quote': 'BTC'}], impact='', importance_score=40,
        importance_reason='影响市场', primary_region={'code': 'CRYPTO', 'name': '加密货币'},
        related_regions=[], assets=[], uncertainties=[], scenarios=[])


async def main():
    assert os.environ['DATABASE_URL'].startswith('postgresql+asyncpg://')
    async with SessionLocal() as session:
        assert await session.scalar(text('SELECT version_num FROM alembic_version')) == 'f63456789012'
        other = aliased(Analysis)
        active = exists(select(1).select_from(other).where(other.news_id == NewsItem.id,
                                                         other.state.in_(('pending', 'processing'))))
        original = await session.scalar(select(Analysis).join(NewsItem).where(
            Analysis.state == 'done', ~active, service.automatic_source()).order_by(Analysis.id).limit(1))
        assert original is not None, '简报隔离样本缺少无在途任务的成功新闻'
        news = await session.get(NewsItem, original.news_id)
        settings = await config.settings_row(session, lock=True)
        revision = settings.control_revision
        version = await session.scalar(select(func.max(Analysis.version_no)).where(Analysis.news_id == news.id))
        article = {'title': 'BTC', 'summary': '研究', 'content_text': 'Unique deadline. ' + '段' * 1_700_000,
                   'source_code': news.source_code, 'url': news.url,
                   'published_at': news.published_at.isoformat() if news.published_at else None,
                   'revision_no': news.revision_no, 'content_hash': news.content_hash,
                   'keyword_candidates': {'items': [], 'matched_count': 0, 'truncated': False}}
        rules = {'schema': 'synthetic:analysis:v1', 'skill': 'synthetic'}
        first = Analysis(news_id=news.id, version_no=version + 1, state='processing', lane='live',
                         control_revision=revision, effective_at=original.effective_at, time_basis=original.time_basis,
                         input=article, rules=rules, content_hash=news.content_hash, source_revision=news.revision_no)
        session.add(first)
        await session.commit()
        identifier = first.id
    provider, system, _ = await fake_provider()
    attempts = []

    async def interrupted(source, prompt, payload, schema, *, allow_repair=True):
        attempts.append(schema.__name__)
        if schema is analysis_parts.MapResult:
            if len(attempts) == 3:
                raise asyncio.CancelledError()
            mapped = map_result()
            if payload['segment']['start'] == 0:
                mapped.facts.append(analysis_parts.MapFact(text='有截止日', evidence={
                    'field': 'content_text', 'start': 1, 'end': 17, 'quote': 'Unique deadline.'}))
            return mapped, {'input_tokens': 3, 'output_tokens': 1}
        raise AssertionError('final 不应在取消后的片段前调用')

    async def owns(session, row, revision):
        return row.state == 'processing' and row.control_revision == revision

    with patch.object(analysis_parts.keywords, 'candidates', return_value=article['keyword_candidates']), \
         patch.object(submission, 'complete', interrupted):
        async with SessionLocal() as session:
            row = await session.get(Analysis, identifier)
            try:
                await analysis_execution.run(session, row, revision, provider, system, article, rules, owns)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError('合成中断未发生')
    async with SessionLocal() as session:
        first = await session.get(Analysis, identifier)
        saved = list(await session.scalars(select(AnalysisPart).where(AnalysisPart.analysis_id == identifier,
                                AnalysisPart.stage == 'map').order_by(AnalysisPart.part_index)))
        assert 1 < len(saved) <= 32
        assert [part.state for part in saved[:3]] == ['done', 'done', 'processing']
        correction = {'path': 'facts[1].evidence', 'field': 'content_text',
                      'original': {'start': 1, 'end': 17}, 'corrected': {'start': 0, 'end': 16}}
        assert saved[0].evidence_corrections == [correction]
        assert saved[0].output['facts'][1]['evidence']['start'] == 0
        assert saved[1].evidence_corrections is None
        assert first.usage == {'rounds': [{'input_tokens': 3, 'output_tokens': 1}] * 2}, first.usage
        assert all(part.input and part.input_hash and part.rules for part in saved)
        assert saved[-1].end_offset == len(article['content_text'])
        await service.recover(session)
        await session.commit()
        await service.recover(session)
        await session.commit()
        first = await session.get(Analysis, identifier)
        assert first.state == 'interrupted' and first.usage['rounds'][-1] is None
        assert len(first.usage['rounds']) == 3
        assert (await session.get(AnalysisPart, saved[2].id)).usage == {'rounds': [None]}
        second_id = await service.retry_analysis(session, identifier, frozen=True)
        second = await session.get(Analysis, second_id)
        assert second.input == article and second.rules == {'frozen_from': identifier}
        try:
            await service.retry_analysis(session, identifier, frozen=True)
        except ValueError as error:
            assert '其他分析在处理' in str(error)
        else:
            raise AssertionError('冻结重试错误复用了另一个在途版本')
        second.state, second.control_revision = 'processing', revision
        await session.commit()

    retry_calls = []
    async def completed(source, prompt, payload, schema, *, allow_repair=True):
        retry_calls.append(schema.__name__)
        if schema is analysis_parts.MapResult:
            return map_result(), {'input_tokens': 5, 'output_tokens': 2}
        assert schema is contracts.NewsAnalysisDraft
        return final_result(), {'input_tokens': 9, 'output_tokens': 3}

    with patch.object(analysis_parts.keywords, 'candidates', return_value=article['keyword_candidates']), \
         patch.object(submission, 'complete', completed):
        async with SessionLocal() as session:
            second = await session.get(Analysis, second_id)
            result, audit, usage = await analysis_execution.run(session, second, revision, provider, system, article,
                {**rules, 'frozen_from': identifier}, owns)
            assert result.facts[0].quote == 'BTC' and audit['processing_version'] == 'keywords:v3'
            second.output, second.usage, second.state = result.model_dump(), usage, 'done'
            await session.commit()
    async with SessionLocal() as session:
        parts = list(await session.scalars(select(AnalysisPart).where(AnalysisPart.analysis_id == second_id,
                                             AnalysisPart.stage == 'map').order_by(AnalysisPart.part_index)))
        reused = [part for part in parts if part.reused_from is not None]
        assert [part.part_index for part in reused] == [0, 1]
        assert reused[0].evidence_corrections == [correction]
        assert reused[0].output['facts'][1]['evidence']['start'] == 0
        assert reused[1].evidence_corrections is None
        assert all(part.usage is None for part in reused)
        assert len(retry_calls) == len(parts) - 2 + 1
        second = await session.get(Analysis, second_id)
        assert second.state == 'done' and len(second.usage['rounds']) == len(retry_calls)
        assert second.usage['rounds'][-1] == {'input_tokens': 9, 'output_tokens': 3}
        assert await session.scalar(select(func.count()).select_from(AnalysisPart)
                                    .where(AnalysisPart.analysis_id == second_id, AnalysisPart.state == 'processing')) == 0
        short = {**article, 'content_text': '新的短文正文'}
        third = Analysis(news_id=second.news_id, version_no=second.version_no + 1, state='processing', lane='live',
            control_revision=revision, effective_at=second.effective_at, time_basis=second.time_basis,
            input=short, rules=rules, content_hash=second.content_hash, source_revision=second.source_revision)
        session.add(third)
        await session.commit()
        third_id = third.id

    correction_calls = []
    async def interrupted_correction(source, prompt, payload, schema, *, allow_repair=True):
        correction_calls.append(payload)
        assert schema is contracts.NewsAnalysisDraft
        if 'correction' in payload:
            raise asyncio.CancelledError()
        first = final_result()
        first.facts[0].quote = '无此引文'
        return first, {'input_tokens': 11, 'output_tokens': 3}

    with patch.object(submission, 'complete', interrupted_correction):
        async with SessionLocal() as session:
            third = await session.get(Analysis, third_id)
            try:
                await analysis_execution.run(session, third, revision, provider, system, short, rules, owns)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError('纠错的合成中断未发生')
    assert len(correction_calls) == 2 and correction_calls[1]['correction'] is True
    async with SessionLocal() as session:
        third = await session.get(Analysis, third_id)
        steps = {part.stage: part for part in await session.scalars(select(AnalysisPart)
                                              .where(AnalysisPart.analysis_id == third_id))}
        known = {'rounds': [{'input_tokens': 11, 'output_tokens': 3}]}
        assert third.usage == known
        assert steps['final'].state == 'waiting_repair' and steps['final'].usage == known
        assert steps['final'].output['facts'][0]['quote'] == '无此引文'
        assert steps['repair'].state == 'processing'
        await service.recover(session)
        await session.commit()
        await service.recover(session)
        await session.commit()
        third = await session.get(Analysis, third_id)
        assert third.state == 'interrupted' and third.usage['rounds'] == [known['rounds'][0], None]
        assert (await session.get(AnalysisPart, steps['final'].id)).usage == known
        assert (await session.get(AnalysisPart, steps['repair'].id)).usage == {'rounds': [None]}
        for part in steps.values():
            assert (await session.get(AnalysisPart, part.id)).state == 'interrupted'
    await engine.dispose()
    print('Analysis parts acceptance passed: interruption, frozen reuse, correction and usage', flush=True)


async def fake_provider():
    from types import SimpleNamespace as N
    return N(id=1, protocol='openai', base_url='http://127.0.0.1:1', model='synthetic',
             reasoning_effort=None, max_tokens=8192, context_window=250000), 'synthetic skill', {}


if __name__ == '__main__':
    asyncio.run(main())
