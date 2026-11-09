"""环境变量页：模型接入配置。密钥只写不读，接口只返回末 4 位"""

import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.ai import llm
from alphapulse.core.auth import require_admin
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import get_session
from alphapulse.models import AiProvider

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_admin)])

Protocol = Literal["openai", "anthropic"]
# 从页面粘贴时容易带上首尾空格，模型名带空格会被上游当成另一个模型（404）
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Model = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class ProviderOut(BaseModel):
    id: int
    name: str
    protocol: Protocol
    base_url: str
    api_key_hint: str
    model: str
    max_tokens: int
    context_window: int
    reasoning_effort: llm.Effort | None
    is_default: bool
    updated_at: datetime


class ProviderIn(BaseModel):
    name: Name
    protocol: Protocol
    base_url: HttpUrl
    api_key: Annotated[str, Field(min_length=1)]
    model: Model
    max_tokens: Annotated[int, Field(ge=256, le=128000)] = 8192
    context_window: Annotated[int, Field(ge=4096, le=2000000)] = 200000
    reasoning_effort: llm.Effort | None = None
    is_default: bool = False


class ProviderPatch(BaseModel):
    name: Name | None = None
    protocol: Protocol | None = None
    base_url: HttpUrl | None = None
    # 为空表示不修改
    api_key: str | None = None
    model: Model | None = None
    max_tokens: Annotated[int | None, Field(ge=256, le=128000)] = None
    context_window: Annotated[int | None, Field(ge=4096, le=2000000)] = None
    # 显式传 null 表示不传思考强度
    reasoning_effort: llm.Effort | None = None
    # 只能将另一模型设为默认，不能直接取消当前默认模型。
    is_default: bool | None = None


class TestResult(BaseModel):
    ok: bool
    elapsed_ms: int
    reply: str | None = None
    error: str | None = None


def key_fields(api_key: str) -> dict:
    api_key = api_key.strip()
    return {"api_key_encrypted": encrypt(api_key), "api_key_hint": api_key[-4:]}


async def get_provider(session: AsyncSession, provider_id: int) -> AiProvider:
    provider = await session.get(AiProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型配置不存在")
    return provider


async def commit_unique(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "名称已存在") from None


@router.get("/providers")
async def list_providers(session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[ProviderOut]]:
    rows = await session.scalars(select(AiProvider).order_by(AiProvider.id))
    return success([ProviderOut.model_validate(p, from_attributes=True) for p in rows])


@router.post("/providers", status_code=status.HTTP_201_CREATED)
async def create_provider(body: ProviderIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[ProviderOut]:
    # 第一套配置自动成为默认
    is_default = body.is_default or await session.scalar(select(AiProvider.id).limit(1)) is None
    if is_default:
        await session.execute(update(AiProvider).values(is_default=False))
    provider = AiProvider(
        **body.model_dump(exclude={"api_key", "base_url", "is_default"}),
        base_url=str(body.base_url), is_default=is_default, **key_fields(body.api_key),
    )
    session.add(provider)
    await commit_unique(session)
    return success(ProviderOut.model_validate(provider, from_attributes=True))


@router.get('/providers/{provider_id}/models')
async def list_provider_models(provider_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[str]]:
    provider = await get_provider(session, provider_id)
    try:
        return success(await llm.list_models(provider))
    except llm.LlmError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error


@router.patch("/providers/{provider_id}")
async def patch_provider(provider_id: int, body: ProviderPatch, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[ProviderOut]:
    provider = await get_provider(session, provider_id)
    if body.is_default is False and provider.is_default:
        raise HTTPException(status.HTTP_409_CONFLICT, "不能直接取消默认模型，请将其他模型设为默认")
    values = body.model_dump(exclude_unset=True, exclude={"api_key", "base_url", "is_default"})
    # reasoning_effort 显式传 null 表示清除，其他字段为 null 时忽略
    for field, value in values.items():
        if value is not None or field == "reasoning_effort":
            setattr(provider, field, value)
    if body.base_url is not None:
        provider.base_url = str(body.base_url)
    if body.api_key and body.api_key.strip():
        for field, value in key_fields(body.api_key).items():
            setattr(provider, field, value)
    if body.is_default:
        await session.execute(update(AiProvider).where(AiProvider.id != provider_id).values(is_default=False))
        provider.is_default = True
    await commit_unique(session)
    await session.refresh(provider)
    return success(ProviderOut.model_validate(provider, from_attributes=True))


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[None]:
    provider = await get_provider(session, provider_id)
    from alphapulse.news_research.models import ResearchSettings
    research = await session.scalar(select(ResearchSettings).where(
        ResearchSettings.config.contains({'index': {'provider_id': provider_id}}) |
        ResearchSettings.config.contains({'research': {'provider_id': provider_id}})).with_for_update())
    if research:
        raise HTTPException(409, '请先切换两阶段新闻研究使用的接入配置')
    await session.delete(provider)
    await session.flush()
    if provider.is_default:
        # 删掉默认配置后，让最早的一套成为默认，避免 Chat 没有可用配置
        first = await session.scalar(select(AiProvider).order_by(AiProvider.id).limit(1))
        if first:
            first.is_default = True
    await session.commit()
    return success(None)


@router.get("/provider-availability")
async def provider_availability(session: Annotated[AsyncSession, Depends(get_session)]):
    providers = list(await session.scalars(select(AiProvider).order_by(AiProvider.id)))
    try:
        return success([{'provider_id': p.id, 'name': p.name, **await llm.availability(p)} for p in providers])
    except llm.LlmError as error:
        raise HTTPException(503, str(error)) from error


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[TestResult]:
    provider = await get_provider(session, provider_id)
    started = time.monotonic()
    token = llm.fail_fast.set(True)
    try:
        completion = None
        async for event in llm.stream(provider, "只回复 OK。", [{"role": "user", "text": "ping"}], [], provider.reasoning_effort):
            if isinstance(event, llm.Completion):
                completion = event
    except llm.LlmError as e:
        return success(TestResult(ok=False, elapsed_ms=round((time.monotonic() - started) * 1000), error=str(e)))
    finally:
        llm.fail_fast.reset(token)
    return success(TestResult(ok=True, elapsed_ms=round((time.monotonic() - started) * 1000), reply=completion.text[:200]))
