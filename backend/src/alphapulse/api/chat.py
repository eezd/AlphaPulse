"""Chat：单一主会话 + CONTEXT.md。生成过程以 SSE 推送；浏览器断开即停止生成"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.ai import llm
from alphapulse.ai.prompt import build_system_prompt, current_context, estimate_tokens
from alphapulse.ai.tools import ToolOutcome, run_tool, tool_specs
from alphapulse.core.auth import require_admin
from alphapulse.core.db import SessionLocal, get_session
from alphapulse.models import AiProvider, ChatMessage, ChatSession, ContextVersion

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_admin)])

# 一轮回答中最多调用工具的轮数，超出后不再提供工具，让模型给出当前结论
MAX_TOOL_ROUNDS = 8
# 发给前端展示的工具结果长度
TOOL_PREVIEW_CHARS = 2000
STOPPED_RESULT = json.dumps({"error": "用户已停止生成，工具未执行"}, ensure_ascii=False)

# api 只有一个进程：同一时间只允许一轮生成，Reset 也要等生成结束
generating = asyncio.Lock()


class MessageOut(BaseModel):
    id: int
    role: Literal["user", "assistant", "tool"]
    content: dict
    provider_id: int | None
    usage: dict | None
    created_at: datetime


class SessionOut(BaseModel):
    id: int
    created_at: datetime
    messages: list[MessageOut]
    # 下一轮的 prompt 估算（不含新输入），前端据此提示是否接近上下文上限
    estimated_tokens: int
    generating: bool


class SendIn(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=20000)]
    provider_id: int | None = None
    # 本轮思考强度；为空时用模型配置里的默认值
    reasoning_effort: llm.Effort | None = None


class ResetIn(BaseModel):
    clear_context: bool = False


class ContextOut(BaseModel):
    id: int | None
    content: str
    source: str | None
    created_at: datetime | None


class ContextIn(BaseModel):
    content: str


def event(name: str, data: dict) -> ServerSentEvent:
    return ServerSentEvent(raw_data=json.dumps(data, ensure_ascii=False, default=str), event=name)


def neutral(m: ChatMessage) -> dict:
    return {"role": m.role, **m.content}


async def active_session(session: AsyncSession) -> ChatSession:
    current = await session.scalar(select(ChatSession).where(ChatSession.ended_at.is_(None)).order_by(ChatSession.id.desc()).limit(1))
    if current is None:
        current = ChatSession()
        session.add(current)
        await session.commit()
    return current


async def session_messages(session: AsyncSession, session_id: int) -> list[ChatMessage]:
    return list((await session.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id))).all())


async def save_messages(*messages: ChatMessage) -> None:
    async with SessionLocal() as session:
        session.add_all(messages)
        await session.commit()


async def save_shielded(*messages: ChatMessage) -> None:
    """停止生成（请求被取消）时仍要落库，保证历史完整：屏蔽取消再写"""
    with anyio.CancelScope(shield=True):
        await save_messages(*messages)


@router.get("/session")
async def get_chat_session(session: Annotated[AsyncSession, Depends(get_session)]) -> SessionOut:
    # 先取生成状态再读消息：反过来的话，读消息后生成恰好结束，会返回「未在生成」却缺最后一条
    is_generating = generating.locked()
    current = await active_session(session)
    messages = await session_messages(session, current.id)
    system = await build_system_prompt(session)
    history = json.dumps([neutral(m) for m in messages], ensure_ascii=False)
    return SessionOut(
        id=current.id, created_at=current.created_at,
        messages=[MessageOut.model_validate(m, from_attributes=True) for m in messages],
        estimated_tokens=estimate_tokens(system) + estimate_tokens(history),
        generating=is_generating,
    )


@router.post("/messages", response_class=EventSourceResponse)
async def send_message(body: SendIn) -> AsyncIterator[ServerSentEvent]:
    """事件：prompt、thinking、delta、tool_call、tool_result、skill_draft、context_updated、usage、error、done"""
    if generating.locked():
        yield event("error", {"message": "上一轮还在生成，请等待完成或先停止"})
        return
    async with generating:
        async with SessionLocal() as session:
            provider = await session.get(AiProvider, body.provider_id) if body.provider_id else await session.scalar(
                select(AiProvider).where(AiProvider.is_default)
            )
            if provider is None:
                yield event("error", {"message": "没有可用的模型配置，请先到「环境变量」页面添加"})
                return
            session.expunge(provider)
            current = await active_session(session)
            system = await build_system_prompt(session)
            history = [neutral(m) for m in await session_messages(session, current.id)]

        user = {"role": "user", "text": body.text}
        messages = [*history, user]
        estimated = estimate_tokens(system) + estimate_tokens(json.dumps(messages, ensure_ascii=False))
        yield event("prompt", {"estimated_tokens": estimated, "context_window": provider.context_window, "provider": provider.name})
        if estimated + provider.max_tokens > provider.context_window:
            yield event("error", {"message": f"上下文约 {estimated} token，加上最大输出 {provider.max_tokens} 超过上限 {provider.context_window}。请 Reset 或精简 skills / CONTEXT.md"})
            return
        await save_messages(ChatMessage(session_id=current.id, role="user", content={"text": body.text}))

        specs = tool_specs()
        effort = body.reasoning_effort or provider.reasoning_effort
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            text = ""
            completion: llm.Completion | None = None
            try:
                async for item in llm.stream(provider, system, messages, specs if round_no < MAX_TOOL_ROUNDS else [], effort):
                    if isinstance(item, llm.TextDelta):
                        text += item.text
                        yield event("delta", {"text": item.text})
                    elif isinstance(item, llm.ThinkingStarted):
                        yield event("thinking", {})
                    else:
                        completion = item
            except llm.LlmError as e:
                if text:
                    await save_messages(ChatMessage(session_id=current.id, role="assistant", content={"text": text, "tool_calls": [], "error": str(e)}, provider_id=provider.id))
                yield event("error", {"message": str(e)})
                return
            except (asyncio.CancelledError, GeneratorExit):
                # 停止生成：保留已输出的部分，不保留未完成的工具调用。
                # 取消发生在 await 时是 CancelledError，发生在 yield 处（生成器被关闭）时是 GeneratorExit
                await save_shielded(ChatMessage(session_id=current.id, role="assistant", content={"text": text, "tool_calls": [], "stopped": True}, provider_id=provider.id))
                raise

            assistant = {"text": completion.text, "tool_calls": completion.tool_calls}
            if completion.thinking_blocks:
                assistant["thinking_blocks"] = completion.thinking_blocks
            truncated_message = f"模型输出达到最大输出上限（{provider.max_tokens} token）被截断，可在「环境变量」页调大「最大输出」"
            if completion.truncated and not completion.tool_calls:
                assistant["error"] = truncated_message
            await save_messages(ChatMessage(session_id=current.id, role="assistant", content=assistant, provider_id=provider.id, usage=completion.usage))
            messages.append({"role": "assistant", **assistant})
            yield event("usage", completion.usage)
            if not completion.tool_calls:
                if completion.truncated:
                    yield event("error", {"message": truncated_message})
                break

            pending = list(completion.tool_calls)
            try:
                while pending:
                    call = pending[0]
                    yield event("tool_call", {"id": call["id"], "name": call["name"], "arguments": call["arguments"]})
                    # 截断时不执行，把原因告诉模型，让它精简后重试
                    outcome = ToolOutcome(llm.TRUNCATED_RESULT) if completion.truncated else await run_tool(call["name"], call["arguments"])
                    result = {"tool_call_id": call["id"], "name": call["name"], "result": outcome.result}
                    await save_messages(ChatMessage(session_id=current.id, role="tool", content=result))
                    pending.pop(0)
                    messages.append({"role": "tool", **result})
                    yield event("tool_result", {"id": call["id"], "name": call["name"], "preview": outcome.result[:TOOL_PREVIEW_CHARS]})
                    if outcome.event:
                        yield event(*outcome.event)
            except (asyncio.CancelledError, GeneratorExit):
                # 每个工具调用都要有结果，否则下一轮发给模型的历史不合法
                await save_shielded(*(
                    ChatMessage(session_id=current.id, role="tool", content={"tool_call_id": c["id"], "name": c["name"], "result": STOPPED_RESULT})
                    for c in pending
                ))
                raise
        yield event("done", {})


@router.post("/reset")
async def reset(body: ResetIn, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    """结束当前会话（消息保留在库里，不再展示），可选同时清空 CONTEXT.md（写入空版本，可找回）"""
    if generating.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "正在生成，请先停止")
    await session.execute(update(ChatSession).where(ChatSession.ended_at.is_(None)).values(ended_at=datetime.now(UTC)))
    if body.clear_context:
        session.add(ContextVersion(content="", source="reset"))
    await session.commit()


@router.get("/context")
async def get_context(session: Annotated[AsyncSession, Depends(get_session)]) -> ContextOut:
    current = await current_context(session)
    if current is None:
        return ContextOut(id=None, content="", source=None, created_at=None)
    return ContextOut.model_validate(current, from_attributes=True)


@router.put("/context")
async def put_context(body: ContextIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ContextOut:
    version = ContextVersion(content=body.content, source="user")
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return ContextOut.model_validate(version, from_attributes=True)


@router.get("/context/versions")
async def context_versions(session: Annotated[AsyncSession, Depends(get_session)]) -> list[ContextOut]:
    rows = await session.scalars(select(ContextVersion).order_by(ContextVersion.id.desc()).limit(100))
    return [ContextOut.model_validate(v, from_attributes=True) for v in rows]
