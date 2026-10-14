"""两种协议的模型适配：OpenAI Chat Completions 与 Anthropic Messages，统一成流式事件

消息使用与协议无关的格式（与 ChatMessage.content 一致，外加 role）：
  {"role": "user", "text"}
  {"role": "assistant", "text", "tool_calls": [{"id", "name", "arguments"}]}
  {"role": "tool", "tool_call_id", "name", "result"}
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anthropic
import openai

from alphapulse.core.crypto import decrypt
from alphapulse.models import AiProvider

TIMEOUT_SECONDS = 120
# SDK 自带重试只针对连接错误、429 和 5xx
MAX_RETRIES = 2


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


@dataclass
class TextDelta:
    text: str


def api_root(base_url: str) -> str:
    """用户可能填 https://host 或 https://host/v1，统一成不带 /v1 的根地址"""
    root = base_url.rstrip("/")
    return root.removesuffix("/v1")


async def stream(provider: AiProvider, system: str, messages: list[dict], tools: list[ToolSpec]) -> AsyncIterator[TextDelta | Completion]:
    """先逐段产出 TextDelta，最后产出一个 Completion"""
    try:
        if provider.protocol == "openai":
            async for event in _stream_openai(provider, system, messages, tools):
                yield event
        else:
            async for event in _stream_anthropic(provider, system, messages, tools):
                yield event
    except (openai.APIStatusError, anthropic.APIStatusError) as e:
        raise LlmError(f"模型接口返回 {e.status_code}：{e.message}") from e
    except (openai.APITimeoutError, anthropic.APITimeoutError) as e:
        raise LlmError(f"模型接口超时（{TIMEOUT_SECONDS}s）") from e
    except (openai.APIConnectionError, anthropic.APIConnectionError) as e:
        raise LlmError(f"无法连接模型接口：{e}") from e


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


async def _stream_openai(provider: AiProvider, system: str, messages: list[dict], tools: list[ToolSpec]) -> AsyncIterator[TextDelta | Completion]:
    client = openai.AsyncOpenAI(
        api_key=decrypt(provider.api_key_encrypted), base_url=f"{api_root(provider.base_url)}/v1",
        timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES,
    )
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
    if provider.temperature is not None:
        kwargs["temperature"] = provider.temperature
    result = Completion()
    # 按 index 拼接分片到达的工具调用
    calls: dict[int, dict] = {}
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
    result.tool_calls = [_parse_call(c["id"], c["name"], c["arguments"]) for _, c in sorted(calls.items())]
    yield result


# ---------- Anthropic ----------

def _anthropic_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            blocks: list[dict] = [{"type": "text", "text": m["text"]}] if m["text"] else []
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


async def _stream_anthropic(provider: AiProvider, system: str, messages: list[dict], tools: list[ToolSpec]) -> AsyncIterator[TextDelta | Completion]:
    client = anthropic.AsyncAnthropic(
        api_key=decrypt(provider.api_key_encrypted), base_url=api_root(provider.base_url),
        timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES,
    )
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
    if provider.temperature is not None:
        # 当前 SDK 不再提供 temperature 参数，按原始字段透传
        kwargs["extra_body"] = {"temperature": provider.temperature}
    result = Completion()
    blocks: dict[int, dict] = {}
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
            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    result.text += event.delta.text
                    yield TextDelta(event.delta.text)
                elif event.delta.type == "input_json_delta":
                    blocks[event.index]["arguments"] += event.delta.partial_json
            elif event.type == "message_delta":
                result.usage["output_tokens"] = event.usage.output_tokens
    result.tool_calls = [_parse_call(b["id"], b["name"], b["arguments"]) for _, b in sorted(blocks.items())]
    yield result


def _parse_call(call_id: str, name: str, arguments: str) -> dict:
    try:
        parsed = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as e:
        raise LlmError(f"模型返回的工具参数不是合法 JSON：{name} {arguments[:200]}") from e
    return {"id": call_id, "name": name, "arguments": parsed}
