"""环境变量页：模型接入配置。api_key 只写不读，接口只返回末 4 位"""

import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.ai import llm
from alphapulse.core.auth import require_admin
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import get_session
from alphapulse.models import AiProvider

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_admin)])

Protocol = Literal["openai", "anthropic"]


class ProviderOut(BaseModel):
    id: int
    name: str
    protocol: Protocol
    base_url: str
    api_key_hint: str
    model: str
    max_tokens: int
    context_window: int
    temperature: float | None
    is_default: bool
    updated_at: datetime


class ProviderIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    protocol: Protocol
    base_url: HttpUrl
    api_key: Annotated[str, Field(min_length=1)]
    model: Annotated[str, Field(min_length=1, max_length=128)]
    max_tokens: Annotated[int, Field(ge=256, le=128000)] = 8192
    context_window: Annotated[int, Field(ge=4096, le=2000000)] = 200000
    temperature: Annotated[float | None, Field(ge=0, le=2)] = None
    is_default: bool = False


class ProviderPatch(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    protocol: Protocol | None = None
    base_url: HttpUrl | None = None
    # 为空表示不修改
    api_key: str | None = None
    model: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    max_tokens: Annotated[int | None, Field(ge=256, le=128000)] = None
    context_window: Annotated[int | None, Field(ge=4096, le=2000000)] = None
    temperature: Annotated[float | None, Field(ge=0, le=2)] = None
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
async def list_providers(session: Annotated[AsyncSession, Depends(get_session)]) -> list[ProviderOut]:
    rows = await session.scalars(select(AiProvider).order_by(AiProvider.id))
    return [ProviderOut.model_validate(p, from_attributes=True) for p in rows]


@router.post("/providers", status_code=status.HTTP_201_CREATED)
async def create_provider(body: ProviderIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ProviderOut:
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
    return ProviderOut.model_validate(provider, from_attributes=True)


@router.patch("/providers/{provider_id}")
async def patch_provider(provider_id: int, body: ProviderPatch, session: Annotated[AsyncSession, Depends(get_session)]) -> ProviderOut:
    provider = await get_provider(session, provider_id)
    values = body.model_dump(exclude_unset=True, exclude={"api_key", "base_url", "is_default"})
    # temperature 显式传 null 表示清除，其他字段为 null 时忽略
    for field, value in values.items():
        if value is not None or field == "temperature":
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
    return ProviderOut.model_validate(provider, from_attributes=True)


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    provider = await get_provider(session, provider_id)
    await session.delete(provider)
    await session.flush()
    if provider.is_default:
        # 删掉默认配置后，让最早的一套成为默认，避免 Chat 没有可用配置
        first = await session.scalar(select(AiProvider).order_by(AiProvider.id).limit(1))
        if first:
            first.is_default = True
    await session.commit()


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> TestResult:
    provider = await get_provider(session, provider_id)
    started = time.monotonic()
    try:
        completion = None
        async for event in llm.stream(provider, "只回复 OK。", [{"role": "user", "text": "ping"}], []):
            if isinstance(event, llm.Completion):
                completion = event
    except llm.LlmError as e:
        return TestResult(ok=False, elapsed_ms=round((time.monotonic() - started) * 1000), error=str(e))
    return TestResult(ok=True, elapsed_ms=round((time.monotonic() - started) * 1000), reply=completion.text[:200])
