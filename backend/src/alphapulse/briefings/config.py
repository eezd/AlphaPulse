"""已发布 Skill 和独立模型参数选择，不修改共享接入配置。"""
from datetime import date, datetime
from types import SimpleNamespace
from typing import Literal
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from alphapulse.models import AiProvider, SkillNode, SkillVersion
from alphapulse.ai.skills import add_version, create_node, load_tree
from alphapulse.briefings.contracts import (
    BASE, IMPORTANCE_POLICY, KEYWORD_POLICY, SKILL_TEMPLATES,
    HighlightReview, NewsAnalysisResult, ReportResult, StrictModel,
)
from alphapulse.briefings.models import BriefingSettings
from alphapulse.briefings.admission import RESEARCH_CODES, RESEARCH_MARKER
from alphapulse.models.news import Source
from alphapulse.briefings.schedule import SHANGHAI, backfill_start
from alphapulse.ai.submission import fits_context


class ModelConfig(StrictModel):
    provider_id: int = Field(gt=0)
    model: str = Field(min_length=1, max_length=128)
    skill_id: int = Field(gt=0)
    reasoning_effort: Literal['low', 'medium', 'high', 'xhigh', 'max'] | None = None
    max_tokens: int = Field(ge=1024, le=32768)


class SettingsInput(StrictModel):
    analysis: ModelConfig
    period: ModelConfig
    daily: ModelConfig
    concurrency: int = Field(default=3, ge=1, le=16)
    backfill_start: date | None = None
    base_revision: int = Field(ge=0)

    @field_validator('backfill_start')
    @classmethod
    def validate_backfill_start(cls, value):
        if value is not None and value > datetime.now(SHANGHAI).date():
            raise ValueError('回补起始日期不能晚于北京时间今天')
        return value


async def settings_row(session, *, lock=False):
    await session.execute(insert(BriefingSettings).values(id=1, config={}, enabled=False)
                          .on_conflict_do_nothing(index_elements=['id']))
    query = select(BriefingSettings).where(BriefingSettings.id == 1).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


def normalize_backfill(row):
    """持有设置锁时一次性兼容旧范围，重新扫描统一日期新增的报告窗口。"""
    if 'backfill_start' in row.config:
        return
    start = backfill_start(row.config)
    row.config = {key: value for key, value in row.config.items()
                  if key not in ('period_backfill_start', 'daily_backfill_start')}
    row.config = {**row.config, 'backfill_start': start.isoformat() if start != date.min else None}
    row.history_start = row.history_end = row.history_cursor = None
    row.history_enrolled = False


def system_prompt(content, stage):
    policies = [IMPORTANCE_POLICY] + ([KEYWORD_POLICY] if stage == 'analysis' else [])
    return '\n\n'.join([BASE + '\n' + content, *[policy for policy in policies if policy not in content]])


async def model_config(session, config, stage, *, require_research=False):
    selected = ModelConfig.model_validate(config[stage])
    source = await session.get(AiProvider, selected.provider_id)
    if source is None:
        raise ValueError('模型接入不存在')
    # 只向网络适配传递独立参数，避免 ORM 状态共享污染接入配置。
    provider = SimpleNamespace(**{key: getattr(source, key) for key in (
        'id', 'name', 'protocol', 'base_url', 'api_key_encrypted', 'context_window')})
    provider.model = selected.model.strip()
    if not provider.model:
        raise ValueError('模型名称不能为空')
    provider.reasoning_effort = selected.reasoning_effort
    provider.max_tokens = selected.max_tokens
    node = await session.get(SkillNode, selected.skill_id)
    if node is None or node.scope != 'news' or node.kind != 'skill' or not node.published_version_id:
        raise ValueError('请选择已发布的新闻专用 Skill')
    ancestor = node
    while ancestor is not None:
        if not ancestor.enabled:
            raise ValueError('Skill 或上级文件夹已停用')
        ancestor = await session.get(SkillNode, ancestor.parent_id) if ancestor.parent_id else None
    version = await session.get(SkillVersion, node.published_version_id)
    if version is None or version.skill_id != node.id or f'<!-- briefing:{stage}:v1 -->' not in version.content:
        raise ValueError('Skill 任务契约与当前报表阶段不匹配，请从新模板建立并发布')
    if require_research or await session.scalar(select(Source.code).where(
            Source.code.in_(RESEARCH_CODES), Source.analysis_policy == 'automatic').limit(1)):
        if RESEARCH_MARKER not in version.content:
            raise ValueError('机构研究需要三份已发布 Skill 均支持研究契约，请先发布候选模板')
    system = system_prompt(version.content, stage)
    rules = {'stage': stage, 'importance_policy': 'importance:publication:v1', 'skill_id': node.id, 'skill_version_id': version.id,
             'skill_version': version.version_no, 'system_prompt': system,
             'provider_id': source.id, 'model': provider.model, 'max_tokens': provider.max_tokens,
             'context_window': provider.context_window, 'reasoning_effort': provider.reasoning_effort}
    schema = NewsAnalysisResult if stage == 'analysis' else ReportResult
    if not fits_context(provider, system, {}, schema):
        raise ValueError('上下文无法容纳 Skill、提交结构和输出预算')
    if stage == 'daily' and not fits_context(provider, system, {}, HighlightReview):
        raise ValueError('上下文无法容纳日报重点复核结构')
    return provider, system, rules


async def initialize_skills(session):
    """调用方持有 settings 锁；只新建缺少的契约，已有发布内容不变。"""
    tree = await load_tree(session)
    nodes = {info.node.id: info.node for info in tree}
    versions = {item.id: item for item in await session.scalars(select(SkillVersion).where(
        SkillVersion.id.in_([info.node.published_version_id for info in tree if info.node.published_version_id])))}

    def active(node):
        while node is not None:
            if not node.enabled:
                return False
            node = nodes[node.parent_id] if node.parent_id else None
        return True

    result = []
    for stage, (name, content) in SKILL_TEMPLATES.items():
        chosen = None
        for info in sorted(tree, key=lambda item: (item.node.name != name, item.path)):
            node = info.node
            version = versions.get(node.published_version_id)
            if (node.kind == 'skill' and node.scope == 'news' and active(node) and version
                    and version.skill_id == node.id and f'<!-- briefing:{stage}:v1 -->' in version.content):
                chosen = node
                break
        if chosen is None:
            chosen = await create_node(session, None, 'skill', name, scope='news')
            version = await add_version(session, chosen, content, 'user', '初始化新闻分析与报告模板')
            chosen.published_version_id = version.id
        result.append({'stage': stage, 'skill_id': chosen.id})
    await session.flush()
    return result


async def validate_config(session, config, *, require_research=False):
    for stage in ('analysis', 'period', 'daily'):
        await model_config(session, config, stage, require_research=require_research)
