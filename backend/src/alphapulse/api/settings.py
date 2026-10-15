"""环境变量页：模型接入配置、币安模拟盘 key。密钥只写不读，接口只返回末 4 位"""

import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.ai import llm
from alphapulse.core.auth import require_admin
from alphapulse.core.crypto import decrypt, encrypt
from alphapulse.core.db import get_session
from alphapulse.models import AiProvider, TradingAccount
from alphapulse.trading.client import DemoClient, ExchangeError

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_admin)])

Protocol = Literal["openai", "anthropic"]
# 从页面粘贴时容易带上首尾空格，模型名带空格会被上游当成另一个模型（404）
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Model = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ApiCredential = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


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
        async for event in llm.stream(provider, "只回复 OK。", [{"role": "user", "text": "ping"}], [], provider.reasoning_effort):
            if isinstance(event, llm.Completion):
                completion = event
    except llm.LlmError as e:
        return TestResult(ok=False, elapsed_ms=round((time.monotonic() - started) * 1000), error=str(e))
    return TestResult(ok=True, elapsed_ms=round((time.monotonic() - started) * 1000), reply=completion.text[:200])


# ---------- 币安模拟盘 ----------

class ExchangeOut(BaseModel):
    configured: bool
    api_key_hint: str | None
    running: bool
    updated_at: datetime | None


class ExchangeIn(BaseModel):
    # key 和 secret 成对生效，修改时两个都要填
    api_key: ApiCredential
    api_secret: ApiCredential


class ExchangeTestResult(BaseModel):
    ok: bool
    elapsed_ms: int
    available_usdt: str | None = None
    wallet_usdt: str | None = None
    # 双向持仓模式；引擎使用单向持仓，启动时会切换
    dual_side_position: bool | None = None
    error: str | None = None


def exchange_out(account: TradingAccount | None) -> ExchangeOut:
    if account is None:
        return ExchangeOut(configured=False, api_key_hint=None, running=False, updated_at=None)
    return ExchangeOut(
        configured=account.api_key_encrypted is not None, api_key_hint=account.api_key_hint,
        running=account.running, updated_at=account.updated_at,
    )


async def editable_account(session: AsyncSession) -> TradingAccount:
    account = await session.get(TradingAccount, 1)
    if account is not None and account.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "模拟盘正在交易，请先停止再修改 key")
    return account or TradingAccount(id=1)


@router.get("/exchange")
async def get_exchange(session: Annotated[AsyncSession, Depends(get_session)]) -> ExchangeOut:
    return exchange_out(await session.get(TradingAccount, 1))


@router.put("/exchange")
async def put_exchange(body: ExchangeIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ExchangeOut:
    account = await editable_account(session)
    account.api_key_encrypted = encrypt(body.api_key)
    account.api_secret_encrypted = encrypt(body.api_secret)
    account.api_key_hint = body.api_key[-4:]
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return exchange_out(account)


@router.delete("/exchange")
async def delete_exchange(session: Annotated[AsyncSession, Depends(get_session)]) -> ExchangeOut:
    account = await editable_account(session)
    account.api_key_encrypted = account.api_secret_encrypted = account.api_key_hint = None
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return exchange_out(account)


@router.post("/exchange/test")
async def test_exchange(session: Annotated[AsyncSession, Depends(get_session)]) -> ExchangeTestResult:
    account = await session.get(TradingAccount, 1)
    if account is None or account.api_key_encrypted is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "还没有填写模拟盘 key")
    started = time.monotonic()
    try:
        async with DemoClient(decrypt(account.api_key_encrypted), decrypt(account.api_secret_encrypted)) as client:
            info = await client.request("GET", "/fapi/v3/account")
            mode = await client.request("GET", "/fapi/v1/positionSide/dual")
    except ExchangeError as e:
        hint = "（key 须在币安模拟交易里创建，实盘 key 不能用；或未开启合约权限、服务器 IP 不在白名单）" if e.code == -2015 else ""
        return ExchangeTestResult(ok=False, elapsed_ms=round((time.monotonic() - started) * 1000), error=f"{e}{hint}")
    return ExchangeTestResult(
        ok=True, elapsed_ms=round((time.monotonic() - started) * 1000),
        available_usdt=info["availableBalance"], wallet_usdt=info["totalWalletBalance"],
        dual_side_position=mode["dualSidePosition"],
    )
