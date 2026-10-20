import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import httpx2
import pytest


def install_model_transport(llm, monkeypatch, protocol, handler):
    monkeypatch.setattr(llm, "decrypt", lambda value: "test-only")
    module = llm.openai if protocol == "openai" else llm.anthropic
    name = "AsyncOpenAI" if protocol == "openai" else "AsyncAnthropic"
    constructor = getattr(module, name)
    http = httpx if protocol == "openai" else httpx2
    monkeypatch.setattr(module, name, lambda **kwargs: constructor(**kwargs, http_client=http.AsyncClient(transport=http.MockTransport(handler))))
    return SimpleNamespace(protocol=protocol, base_url="https://model.invalid", api_key_encrypted=b"test", model="test", max_tokens=100)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("complete", [False, True])
def test_model_requires_protocol_end_marker(trading, monkeypatch, protocol, complete):
    from alphapulse.ai import llm
    if protocol == "openai":
        chunks = [{"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "test", "choices": [{"index": 0, "delta": {"content": "部分回答"}, "finish_reason": None}]}]
        if complete:
            chunks.append(chunks[0] | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    else:
        events = [
            {"type": "message_start", "message": {"id": "1", "type": "message", "role": "assistant", "model": "test", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "部分回答"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
        ]
        if complete:
            events.append({"type": "message_stop"})
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)

    def handle(request):
        http = httpx if protocol == "openai" else httpx2
        return http.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    provider = install_model_transport(llm, monkeypatch, protocol, handle)
    async def check():
        output = []
        try:
            async for item in llm.stream(provider, "system", [], []):
                output.append(item)
        except llm.LlmError:
            assert not complete
        else:
            assert complete
        assert [x.text for x in output if isinstance(x, llm.TextDelta)] == ["部分回答"]
        assert any(isinstance(x, llm.Completion) for x in output) == complete
    asyncio.run(check())


def test_connection_failure_uses_only_sdk_retry_budget(trading, monkeypatch):
    from alphapulse.ai import llm
    attempts = 0
    def handle(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("测试连接失败", request=request)
    provider = install_model_transport(llm, monkeypatch, "openai", handle)
    async def check():
        with pytest.raises(llm.LlmError, match="无法连接"):
            async for _ in llm.stream(provider, "system", [], []):
                pass
    asyncio.run(check())
    assert attempts == llm.MAX_RETRIES + 1


def test_pipeline_error_keeps_history_checkpoint_and_marks_run_failed(trading, monkeypatch):
    from scrapy import signals
    from scrapy.crawler import Crawler
    from twisted.python.failure import Failure
    from alphapulse.crawler.spiders import base

    class Spider(base.NewsSpider):
        name = "test"

    now = datetime.now(UTC)
    target, checkpoint = now - timedelta(days=10), now - timedelta(days=1)
    source = SimpleNamespace(history_target=target, history_position=checkpoint, consecutive_failures=0)
    writes = []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, statement):
            writes.append(statement.compile().params)
        async def get(self, *args, **kwargs):
            return source
        async def commit(self):
            pass
    monkeypatch.setattr(base, "SessionLocal", Session)
    async def check():
        crawler = Crawler(Spider)
        spider = Spider.from_crawler(crawler)
        run = base.RunState(uuid.uuid4(), "history", now, target, history_target=target, history_position=target)
        spider.run = run
        crawler.signals.send_catch_log(signals.item_error, item={}, response=None, spider=spider, failure=Failure(RuntimeError("写库失败")))
        spider._close_run()
        await asyncio.gather(*spider._tasks)
        assert spider.history_checkpoint is None and source.history_position == checkpoint
        assert source.consecutive_failures == 1
        assert writes[0]["status"] == "failed" and "写库失败" in writes[0]["error"]
    asyncio.run(check())


@pytest.mark.parametrize("failure_at", ["usage", "second_tool"])
def test_chat_completes_tool_results_on_disconnect_or_exception(trading, monkeypatch, failure_at):
    from alphapulse.api import chat
    provider = SimpleNamespace(id=1, context_window=10000, max_tokens=100, name="test", reasoning_effort=None)
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def scalar(self, *args):
            return provider
        def expunge(self, *args):
            pass
    async def current(*args):
        return SimpleNamespace(id=1)
    async def system(*args):
        return "system"
    async def history(*args):
        return []
    saved = []
    async def save(*messages):
        saved.extend(messages)
    calls = [{"id": "a", "name": "first", "arguments": {}}, {"id": "b", "name": "second", "arguments": {}}]
    async def stream(*args):
        yield chat.llm.Completion(tool_calls=calls)
    async def tool(name, args):
        if name == "second":
            raise RuntimeError("工具失败")
        return chat.ToolOutcome("实际结果")
    monkeypatch.setattr(chat, "SessionLocal", Session)
    monkeypatch.setattr(chat, "active_session", current)
    monkeypatch.setattr(chat, "build_system_prompt", system)
    monkeypatch.setattr(chat, "session_messages", history)
    monkeypatch.setattr(chat, "save_messages", save)
    monkeypatch.setattr(chat.llm, "stream", stream)
    monkeypatch.setattr(chat, "run_tool", tool)
    async def check():
        generator = chat.send_message(chat.SendIn(text="test"))
        events = []
        async for event in generator:
            events.append(event.event)
            if failure_at == "usage" and event.event == "usage":
                await generator.aclose()
                break
        results = [m.content for m in saved if m.role == "tool"]
        assert [r["tool_call_id"] for r in results] == ["a", "b"]
        if failure_at == "second_tool":
            assert results[0]["result"] == "实际结果"
            assert "error" in json.loads(results[1]["result"])
            assert events[-1] == "error"
    asyncio.run(check())


def test_skill_rename_rejects_whitespace_without_writing(trading):
    from fastapi import HTTPException
    from alphapulse.api import skills
    node = SimpleNamespace(name="原名称")
    class Session:
        async def get(self, *args):
            return node
    async def check():
        with pytest.raises(HTTPException) as error:
            await skills.patch(1, skills.NodePatch(name=" \t "), Session())
        assert error.value.status_code == 400
        assert node.name == "原名称"
    asyncio.run(check())


def test_chat_reset_blocks_new_generation_until_commit(trading, monkeypatch):
    from alphapulse.api import chat

    async def check():
        monkeypatch.setattr(chat, "generating", asyncio.Lock())
        entered, finish = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(chat, "SessionLocal", lambda: pytest.fail("重置期间不能进入生成的数据库路径"))
        saved = []
        class Session:
            async def execute(self, statement):
                entered.set()
                await finish.wait()
            def add(self, row):
                saved.append(row)
            async def commit(self):
                assert chat.generating.locked()
        async def reset_and_send():
            task = asyncio.create_task(chat.reset(chat.ResetIn(clear_context=True), Session()))
            try:
                await entered.wait()
                events = [event async for event in chat.send_message(chat.SendIn(text="重置期间的新消息"))]
                assert len(events) == 1 and events[0].event == "error"
                assert not task.done()
            finally:
                finish.set()
                await task
        await asyncio.wait_for(reset_and_send(), timeout=2)
        assert len(saved) == 1 and saved[0].content == "" and saved[0].source == "reset"
        assert not chat.generating.locked()
    asyncio.run(check())


def test_chat_reset_rejects_busy_generation_and_releases_lock_on_failure(trading, monkeypatch):
    from fastapi import HTTPException
    from alphapulse.api import chat

    async def check():
        monkeypatch.setattr(chat, "generating", asyncio.Lock())
        class Session:
            async def execute(self, statement):
                raise RuntimeError("重置写库失败")
        async with chat.generating:
            with pytest.raises(HTTPException) as error:
                await chat.reset(chat.ResetIn(), Session())
            assert error.value.status_code == 409
        with pytest.raises(RuntimeError, match="重置写库失败"):
            await chat.reset(chat.ResetIn(), Session())
        assert not chat.generating.locked()
    asyncio.run(check())
