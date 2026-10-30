"""两种协议的模型适配：OpenAI Chat Completions 与 Anthropic Messages，统一成流式事件

消息使用与协议无关的格式（与 ChatMessage.content 一致，外加 role）：
  {"role": "user", "text"}
  {"role": "assistant", "text", "tool_calls": [{"id", "name", "arguments"}], "thinking_blocks"?: [...]}
  {"role": "tool", "tool_call_id", "name", "result"}
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
import httpx
import httpx2  # anthropic SDK 使用的 HTTP 客户端，异常类与 httpx 不通用
import openai

from alphapulse.core.crypto import decrypt
from alphapulse.models import AiProvider

# 高思考强度时 OpenAI 格式在推理期间不推送任何内容，读超时要留足
TIMEOUT_SECONDS = 600
# 思考强度：两种协议共有的取值。OpenAI 另有 none/minimal，但实测经 Sub2API 会卡住后 502，不提供
Effort = Literal["low", "medium", "high", "xhigh", "max"]
# SDK 自带重试只针对连接错误、429 和 5xx
MAX_RETRIES = 2
# 只重试 SDK 不处理的流内错误和读取中断，且只在还没有输出内容时重试。
STREAM_RETRIES = 3
STREAM_RETRY_DELAY_SECONDS = 2
# 输出被截断时回给模型的工具结果：参数不完整，不能执行
TRUNCATED_RESULT = json.dumps({"error": "输出达到最大输出上限被截断，工具参数不完整，未执行。请精简内容后重新调用"}, ensure_ascii=False)

logger = logging.getLogger("alphapulse.ai.llm")


class LlmError(Exception):
    """模型调用失败；message 可直接展示给用户"""


@dataclass
class ToolSpec:
    name: str
    description: str
    # JSON Schema
    parameters: dict


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    # Anthropic 的 thinking / redacted_thinking 块：工具调用循环中必须原样回传，否则接口报错
    thinking_blocks: list[dict] = field(default_factory=list)
    # 达到 max_tokens 被截断：工具调用的参数可能不完整，不能执行
    truncated: bool = False


@dataclass
class TextDelta:
    text: str


@dataclass
class ThinkingStarted:
    """模型开始思考（只有 Claude 格式会推送）；前端据此显示「思考中」"""


def api_root(base_url: str) -> str:
    """用户可能填 https://host 或 https://host/v1，统一成不带 /v1 的根地址"""
    root = base_url.rstrip("/")
    return root.removesuffix("/v1")


async def list_models(provider: AiProvider) -> list[str]:
    """Read the provider's model catalog without generating a completion."""
    options = {'api_key': decrypt(provider.api_key_encrypted), 'timeout': 10, 'max_retries': 0}
    root = api_root(provider.base_url)
    client = (openai.AsyncOpenAI(base_url=f'{root}/v1', **options) if provider.protocol == 'openai'
              else anthropic.AsyncAnthropic(base_url=root, **options))
    try:
        async with asyncio.timeout(30), client:
            models = await client.models.list()
            identifiers = set()
            async for model in models:
                identifier = getattr(model, 'id', None)
                if not isinstance(identifier, str) or not identifier.strip():
                    raise LlmError('模型列表响应缺少有效的模型 ID；请手动填写模型名称')
                identifiers.add(identifier)
            return sorted(identifiers)
    except (openai.APIStatusError, anthropic.APIStatusError) as error:
        raise LlmError(f'模型列表接口返回 HTTP {error.status_code}；请检查接入权限，或手动填写模型名称') from error
    except (TimeoutError, openai.APITimeoutError, anthropic.APITimeoutError,
            httpx.TimeoutException, httpx2.TimeoutException) as error:
        raise LlmError('读取模型列表超时；可重试或手动填写模型名称') from error
    except (openai.APIError, anthropic.APIError, httpx.TransportError, httpx2.TransportError) as error:
        raise LlmError('无法读取模型列表；请检查接入配置，或手动填写模型名称') from error


async def stream(
    provider: AiProvider, system: str, messages: list[dict], tools: list[ToolSpec], effort: Effort | None = None,
) -> AsyncIterator[TextDelta | ThinkingStarted | Completion]:
    """先逐段产出 TextDelta / ThinkingStarted，最后产出一个 Completion。effort 为空表示不传，由模型决定"""
    for attempt in range(STREAM_RETRIES + 1):
        output = False
        try:
            if provider.protocol == "openai":
                events = _stream_openai(provider, system, messages, tools, effort)
            else:
                events = _stream_anthropic(provider, system, messages, tools, effort)
            async for event in events:
                output = True
                yield event
            return
        # 流式读取过程中的网络错误 SDK 不包装，以原始的 httpx / httpx2 异常抛出
        except (openai.APIError, anthropic.APIError, httpx.TransportError, httpx2.TransportError) as e:
            # 已经推送过内容再重试会重复输出
            if output or attempt == STREAM_RETRIES or not _retryable(e):
                raise _llm_error(e) from e
            logger.warning("模型调用失败，%ss 后第 %s 次重试：%s", STREAM_RETRY_DELAY_SECONDS, attempt + 1, _llm_error(e))
            await asyncio.sleep(STREAM_RETRY_DELAY_SECONDS)


def _in_stream_error(e: Exception) -> bool:
    """连接建立后上游在流内返回的错误事件（如 overloaded）。
    OpenAI SDK 抛 APIError 基类；Anthropic SDK 抛 APIStatusError，状态码是流本身的 200"""
    if isinstance(e, openai.APIError):
        return not isinstance(e, (openai.APIStatusError, openai.APIConnectionError))
    return isinstance(e, anthropic.APIStatusError) and e.status_code == 200


def _retryable(e: Exception) -> bool:
    """建连重试归 SDK；这里只处理流内错误与读取中断，超时不重试。"""
    if isinstance(e, (openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException, httpx2.TimeoutException)):
        return False
    return _in_stream_error(e) or isinstance(
        e, (httpx.TransportError, httpx2.TransportError),
    )


def _llm_error(e: Exception) -> LlmError:
    if _in_stream_error(e):
        error = e.body.get("error") if isinstance(e.body, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        return LlmError(f"模型接口出错：{message or e.message}")
    if isinstance(e, (openai.APIStatusError, anthropic.APIStatusError)):
        return LlmError(f"模型接口返回 {e.status_code}：{e.message}")
    if isinstance(e, (openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException, httpx2.TimeoutException)):
        return LlmError(f"模型接口超时（{TIMEOUT_SECONDS}s）")
    if isinstance(e, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return LlmError(f"无法连接模型接口：{e}")
    if isinstance(e, (httpx.TransportError, httpx2.TransportError)):
        return LlmError(f"模型接口连接中断：{e!r}")
    return LlmError(f"模型接口出错：{e}")


# ---------- OpenAI ----------

def _openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            if not m["text"] and not m.get("tool_calls"):
                # 停止得太早或模型没有输出：空的 assistant 消息会被接口拒绝
                continue
            msg: dict[str, Any] = {"role": "assistant", "content": m["text"] or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                    for c in m["tool_calls"]
                ]
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["result"]})
    return out


async def _stream_openai(provider: AiProvider, system: str, messages: list[dict], tools: list[ToolSpec], effort: Effort | None) -> AsyncIterator[TextDelta | Completion]:
    client = openai.AsyncOpenAI(
        api_key=decrypt(provider.api_key_encrypted), base_url=f"{api_root(provider.base_url)}/v1",
        timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES,
    )
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
    if effort:
        kwargs["reasoning_effort"] = effort
    result = Completion()
    # 按 index 拼接分片到达的工具调用
    calls: dict[int, dict] = {}
    finished = False
    async with client:
        chunks = await client.chat.completions.create(
            model=provider.model, messages=_openai_messages(system, messages), max_tokens=provider.max_tokens,
            stream=True, stream_options={"include_usage": True}, **kwargs,
        )
        async for chunk in chunks:
            if chunk.usage:
                result.usage = {"input_tokens": chunk.usage.prompt_tokens, "output_tokens": chunk.usage.completion_tokens}
            if not chunk.choices:
                continue
            reason = chunk.choices[0].finish_reason
            if reason == "content_filter":
                raise LlmError("模型输出被内容过滤器中止")
            if reason is not None:
                if reason not in ("stop", "tool_calls", "length"):
                    raise LlmError(f"模型返回了不支持的结束原因：{reason}")
                finished = True
            if reason == "length":
                result.truncated = True
            delta = chunk.choices[0].delta
            if delta.content:
                result.text += delta.content
                yield TextDelta(delta.content)
            for tc in delta.tool_calls or []:
                call = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    call["id"] = tc.id
                if tc.function and tc.function.name:
                    call["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    call["arguments"] += tc.function.arguments
    if not finished:
        raise LlmError("模型连接提前结束，未收到完整响应的结束标志")
    result.tool_calls = [_parse_call(c["id"], c["name"], c["arguments"], result.truncated) for _, c in sorted(calls.items())]
    yield result


# ---------- Anthropic ----------

def _anthropic_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            # 思考块必须排在最前，原样回传
            blocks: list[dict] = list(m.get("thinking_blocks") or [])
            if m["text"]:
                blocks.append({"type": "text", "text": m["text"]})
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]} for c in m.get("tool_calls") or []]
            # 空的 assistant 消息会被接口拒绝
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        else:
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["result"]}
            # 同一轮的多个工具结果必须放进同一条 user 消息
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


async def _stream_anthropic(provider: AiProvider, system: str, messages: list[dict], tools: list[ToolSpec], effort: Effort | None) -> AsyncIterator[TextDelta | ThinkingStarted | Completion]:
    client = anthropic.AsyncAnthropic(
        api_key=decrypt(provider.api_key_encrypted), base_url=api_root(provider.base_url),
        timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES,
    )
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
    if effort:
        # 思考强度通过 output_config.effort 控制，配合自适应思考（由模型决定是否、思考多久）
        kwargs["output_config"] = {"effort": effort}
        kwargs["thinking"] = {"type": "adaptive"}
    result = Completion()
    blocks: dict[int, dict] = {}
    thinking: dict[int, dict] = {}
    finished = False
    async with client:
        events = await client.messages.create(
            model=provider.model, system=system, messages=_anthropic_messages(messages), max_tokens=provider.max_tokens,
            stream=True, **kwargs,
        )
        async for event in events:
            if event.type == "message_start":
                result.usage["input_tokens"] = event.message.usage.input_tokens
            elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                blocks[event.index] = {"id": event.content_block.id, "name": event.content_block.name, "arguments": ""}
            elif event.type == "content_block_start" and event.content_block.type == "thinking":
                thinking[event.index] = {"type": "thinking", "thinking": event.content_block.thinking, "signature": event.content_block.signature}
                yield ThinkingStarted()
            elif event.type == "content_block_start" and event.content_block.type == "redacted_thinking":
                thinking[event.index] = {"type": "redacted_thinking", "data": event.content_block.data}
                yield ThinkingStarted()
            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    result.text += event.delta.text
                    yield TextDelta(event.delta.text)
                elif event.delta.type == "input_json_delta":
                    blocks[event.index]["arguments"] += event.delta.partial_json
                elif event.delta.type == "thinking_delta":
                    thinking[event.index]["thinking"] += event.delta.thinking
                elif event.delta.type == "signature_delta":
                    thinking[event.index]["signature"] += event.delta.signature
            elif event.type == "message_delta":
                result.usage["output_tokens"] = event.usage.output_tokens
                result.truncated = event.delta.stop_reason == "max_tokens"
            elif event.type == "message_stop":
                finished = True
    if not finished:
        raise LlmError("模型连接提前结束，未收到完整响应的结束标志")
    result.tool_calls = [_parse_call(b["id"], b["name"], b["arguments"], result.truncated) for _, b in sorted(blocks.items())]
    result.thinking_blocks = [b for _, b in sorted(thinking.items())]
    yield result


def _parse_call(call_id: str, name: str, arguments: str, truncated: bool) -> dict:
    if truncated:
        # 截断的参数是半截 JSON，不解析；调用方不会执行它
        return {"id": call_id, "name": name, "arguments": {}}
    try:
        parsed = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as e:
        raise LlmError(f"模型返回的工具参数不是合法 JSON：{name} {arguments[:200]}") from e
    return {"id": call_id, "name": name, "arguments": parsed}
