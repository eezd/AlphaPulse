"""环境变量页：模型接入配置、交易所模拟盘 key。密钥只写不读，接口只返回末 4 位"""

import time
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, StringConstraints, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.ai import llm
from alphapulse.core.auth import require_admin
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.models import AiProvider, TradingAccount, TradingAnalysis, TradingCredential, TradingSymbol
from alphapulse.models.risk import RiskSettings
from alphapulse.trading.client import ExchangeError, create_client
from alphapulse.trading.snapshot import STATUS_KEY

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
    risk = await session.scalar(select(RiskSettings).where((RiskSettings.provider_id == provider_id) | (RiskSettings.second_provider_id == provider_id) | (RiskSettings.third_provider_id == provider_id)).with_for_update())
    if risk:
        if risk.enabled:
            raise HTTPException(status.HTTP_409_CONFLICT, "请先暂停新闻 AI 分析或切换其接入配置")
        if risk.provider_id == provider_id:
            risk.provider_id = None
        if risk.third_provider_id == provider_id:
            risk.third_provider_id = None
        if risk.second_provider_id == provider_id:
            risk.second_provider_id = None
    await session.delete(provider)
    await session.flush()
    if provider.is_default:
        # 删掉默认配置后，让最早的一套成为默认，避免 Chat 没有可用配置
        first = await session.scalar(select(AiProvider).order_by(AiProvider.id).limit(1))
        if first:
            first.is_default = True
    await session.commit()
    return success(None)


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[TestResult]:
    provider = await get_provider(session, provider_id)
    started = time.monotonic()
    try:
        completion = None
        async for event in llm.stream(provider, "只回复 OK。", [{"role": "user", "text": "ping"}], [], provider.reasoning_effort):
            if isinstance(event, llm.Completion):
                completion = event
    except llm.LlmError as e:
        return success(TestResult(ok=False, elapsed_ms=round((time.monotonic() - started) * 1000), error=str(e)))
    return success(TestResult(ok=True, elapsed_ms=round((time.monotonic() - started) * 1000), reply=completion.text[:200]))


# ---------- 模拟盘 ----------

ExchangeName = Literal["binance", "bybit"]


class ExchangeOut(BaseModel):
    exchange: ExchangeName
    configured_exchanges: list[ExchangeName]
    configured: bool
    api_key_hint: str | None
    running: bool
    updated_at: datetime | None


class ExchangeIn(BaseModel):
    # key 和 secret 成对生效，修改时两个都要填
    exchange: ExchangeName | None = None
    api_key: ApiCredential | None = None
    api_secret: ApiCredential | None = None

    @model_validator(mode="after")
    def paired_credentials(self):
        if (self.api_key is None) != (self.api_secret is None):
            raise ValueError("API Key 和 Secret Key 必须同时填写")
        if self.exchange is None and self.api_key is None:
            raise ValueError("请选择交易所或填写凭证")
        return self


class ExchangeTestResult(BaseModel):
    ok: bool
    elapsed_ms: int
    available_usdt: str | None = None
    wallet_usdt: str | None = None
    # 双向持仓模式；引擎使用单向持仓，启动时会切换
    dual_side_position: bool | None = None
    error: str | None = None


async def any_running(session: AsyncSession) -> bool:
    return bool(await session.scalar(select(func.count()).where(TradingSymbol.running)))


async def exchange_out(session: AsyncSession, account: TradingAccount | None) -> ExchangeOut:
    exchange = account.exchange if account else "binance"
    credential = await session.get(TradingCredential, exchange)
    configured = list(await session.scalars(select(TradingCredential.exchange).where(TradingCredential.api_key_encrypted.is_not(None))))
    return ExchangeOut(
        exchange=exchange, configured_exchanges=configured,
        configured=bool(credential and credential.api_key_encrypted), api_key_hint=credential.api_key_hint if credential else None,
        running=await any_running(session), updated_at=credential.updated_at if credential else None,
    )


async def editable_account(session: AsyncSession) -> TradingAccount:
    account = await session.get(TradingAccount, 1)
    if await any_running(session):
        raise HTTPException(status.HTTP_409_CONFLICT, "有币种正在交易，请先全部停止再修改交易所或 key")
    if await session.scalar(select(func.count()).where(TradingAnalysis.status == "running")):
        raise HTTPException(status.HTTP_409_CONFLICT, "有分析正在进行，请等待完成后再修改交易所或 key")
    return account or TradingAccount(id=1, exchange="binance")


async def ensure_no_positions(session: AsyncSession, exchange: str) -> None:
    credential = await session.get(TradingCredential, exchange)
    if credential and credential.api_key_encrypted:
        try:
            async with create_client(credential) as client:
                positions = await client.positions()
        except ExchangeError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"无法确认当前交易所持仓，请稍后再试：{e}") from None
        if any(Decimal(p["positionAmt"]) != 0 for p in positions):
            raise HTTPException(status.HTTP_409_CONFLICT, "当前模拟账户仍有持仓，请先平仓再切换交易所或修改 key")


@router.get("/exchange")
async def get_exchange(session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[ExchangeOut]:
    return success(await exchange_out(session, await session.get(TradingAccount, 1)))


@router.put("/exchange")
async def put_exchange(body: ExchangeIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[ExchangeOut]:
    account = await editable_account(session)
    await ensure_no_positions(session, account.exchange)
    exchange = body.exchange or account.exchange
    credential = None
    if body.api_key is not None:
        credential = await session.get(TradingCredential, exchange) or TradingCredential(exchange=exchange)
        credential.api_key_encrypted = encrypt(body.api_key)
        credential.api_secret_encrypted = encrypt(body.api_secret)
        credential.api_key_hint = body.api_key[-4:]
        session.add(credential)
    account.exchange = exchange
    session.add(account)
    # 停止状态下仍可能排有手动分析；切换后不能把旧请求交给另一家交易所。
    await session.execute(update(TradingSymbol).values(analysis_requested_at=None, analysis_reasons=[]))
    await session.commit()
    if credential is not None:
        await session.refresh(credential)
    await redis.delete(STATUS_KEY)
    return success(await exchange_out(session, account))


@router.delete("/exchange")
async def delete_exchange(session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[ExchangeOut]:
    account = await editable_account(session)
    await ensure_no_positions(session, account.exchange)
    credential = await session.get(TradingCredential, account.exchange)
    if credential:
        credential.api_key_encrypted = credential.api_secret_encrypted = credential.api_key_hint = None
    await session.commit()
    if credential is not None:
        await session.refresh(credential)
    await redis.delete(STATUS_KEY)
    return success(await exchange_out(session, account))


@router.post("/exchange/test")
async def test_exchange(session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[ExchangeTestResult]:
    account = await session.get(TradingAccount, 1)
    credential = await session.get(TradingCredential, account.exchange) if account else None
    if credential is None or credential.api_key_encrypted is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "还没有填写当前交易所的模拟盘 key")
    started = time.monotonic()
    try:
        async with create_client(credential) as client:
            info = await client.account()
            dual = await client.position_mode()
    except ExchangeError as e:
        hint = "（请在所选交易所的模拟交易账户创建 key，确认合约权限及 IP 白名单；Bybit Testnet 和实盘 key 均不可用）" if e.code in (-2015, 10003, 10005, 10010) else ""
        return success(ExchangeTestResult(ok=False, elapsed_ms=round((time.monotonic() - started) * 1000), error=f"{e}{hint}"))
    return success(ExchangeTestResult(
        ok=True, elapsed_ms=round((time.monotonic() - started) * 1000),
        available_usdt=info["availableBalance"], wallet_usdt=info["totalWalletBalance"], dual_side_position=dual,
    ))
