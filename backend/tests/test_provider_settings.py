"""模型默认配置的接口契约；数据库和认证均隔离。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.mark.parametrize("initial,requested,status", [
    (True, False, 409), (False, False, 200), (False, True, 200), (True, True, 200),
])
def test_patch_provider_default_contract(trading, monkeypatch, initial, requested, status):
    from alphapulse.api import settings
    from alphapulse.api.main import app
    from alphapulse.core.auth import require_admin
    from alphapulse.core.db import get_session

    provider = SimpleNamespace(
        id=1, name="test", protocol="openai", base_url="https://example.test",
        api_key_hint="test", model="test", max_tokens=8192, context_window=200000,
        reasoning_effort=None, is_default=initial, updated_at=datetime.now(UTC),
    )
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), execute=AsyncMock())
    monkeypatch.setattr(settings, "get_provider", AsyncMock(return_value=provider))

    async def isolated_session():
        yield session

    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_session] = isolated_session

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.patch("/api/settings/providers/1", json={"is_default": requested})
        assert response.status_code == status
        body = response.json()
        if status == 409:
            assert body == {"code": 409, "data": None, "message": "不能直接取消默认模型，请将其他模型设为默认"}
            assert provider.is_default is initial
            session.commit.assert_not_awaited()
            session.execute.assert_not_awaited()
        else:
            assert body["code"] == 0 and body["data"]["is_default"] is requested
            session.commit.assert_awaited_once()
            assert session.execute.await_count == int(requested)

    try:
        asyncio.run(check())
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize('enabled', [True, False])
def test_delete_provider_handles_news_risk_reference(trading, monkeypatch, enabled):
    from fastapi import HTTPException
    from alphapulse.api import settings
    reference = SimpleNamespace(enabled=enabled, provider_id=1, second_provider_id=1, third_provider_id=1)
    provider = SimpleNamespace(id=1, is_default=False)
    session = SimpleNamespace(scalar=AsyncMock(return_value=reference), delete=AsyncMock(),
        flush=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(settings, 'get_provider', AsyncMock(return_value=provider))
    if enabled:
        with pytest.raises(HTTPException) as error:
            asyncio.run(settings.delete_provider(1, session))
        assert error.value.status_code == 409
        session.delete.assert_not_awaited()
        assert reference.provider_id == 1
    else:
        asyncio.run(settings.delete_provider(1, session))
        assert reference.provider_id is None
        session.delete.assert_awaited_once_with(provider)
        session.commit.assert_awaited_once()


@pytest.mark.parametrize('protocol,base_url', [('openai', 'https://example.invalid/v1/'), ('anthropic', 'https://example.invalid/')])
def test_model_catalog_uses_selected_protocol_and_reads_all_pages(trading, monkeypatch, protocol, base_url):
    from alphapulse.ai import llm
    options = []

    class Page:
        async def __aiter__(self):
            for identifier in ['model-b', 'model-a', 'model-b']:
                yield SimpleNamespace(id=identifier)

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
            self.models = SimpleNamespace(list=AsyncMock(return_value=Page()))
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    monkeypatch.setattr(llm, 'decrypt', lambda value: 'test-only-key')
    monkeypatch.setattr(llm.openai if protocol == 'openai' else llm.anthropic,
        'AsyncOpenAI' if protocol == 'openai' else 'AsyncAnthropic', Client)
    provider = SimpleNamespace(protocol=protocol, base_url=base_url, api_key_encrypted=b'test')
    assert asyncio.run(llm.list_models(provider)) == ['model-a', 'model-b']
    assert options == [{'base_url': 'https://example.invalid/v1' if protocol == 'openai' else 'https://example.invalid',
        'api_key': 'test-only-key', 'timeout': 10, 'max_retries': 0}]


@pytest.mark.parametrize('failure', ['http', 'timeout', 'invalid_id'])
def test_model_catalog_failures_remain_explicit_without_exposing_upstream_details(trading, monkeypatch, failure):
    from alphapulse.ai import llm

    class Page:
        async def __aiter__(self):
            yield SimpleNamespace(id=None)

    class Client:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(list=AsyncMock(return_value=Page()))
            if failure == 'timeout': self.models.list.side_effect = TimeoutError()
            if failure == 'http':
                response = httpx.Response(401, request=httpx.Request('GET', 'https://example.invalid/v1/models'))
                self.models.list.side_effect = llm.openai.APIStatusError('private-upstream-details', response=response, body=None)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    monkeypatch.setattr(llm, 'decrypt', lambda value: 'test-only-key')
    monkeypatch.setattr(llm.openai, 'AsyncOpenAI', Client)
    provider = SimpleNamespace(protocol='openai', base_url='https://example.invalid', api_key_encrypted=b'test')
    with pytest.raises(llm.LlmError) as error:
        asyncio.run(llm.list_models(provider))
    assert 'private-upstream-details' not in str(error.value)
    assert '手动填写' in str(error.value)


@pytest.mark.parametrize('failure', [None, 'unavailable', 'missing'])
def test_provider_model_catalog_http_contract(trading, monkeypatch, failure):
    from fastapi import FastAPI, HTTPException
    from alphapulse.api import settings
    provider = SimpleNamespace(id=1)

    async def get_provider(session, identifier):
        assert identifier == 1
        if failure == 'missing': raise HTTPException(404, '模型配置不存在')
        return provider

    catalog = AsyncMock(return_value=['model-a', 'model-b'])
    if failure == 'unavailable': catalog.side_effect = settings.llm.LlmError('读取模型列表失败')
    monkeypatch.setattr(settings, 'get_provider', get_provider)
    monkeypatch.setattr(settings.llm, 'list_models', catalog)
    app = FastAPI()
    app.include_router(settings.router)
    app.dependency_overrides[settings.require_admin] = lambda: None
    app.dependency_overrides[settings.get_session] = lambda: None

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get('/api/settings/providers/1/models')
            assert response.status_code == ({None: 200, 'unavailable': 502, 'missing': 404}[failure])
            if failure is None: assert response.json()['data'] == ['model-a', 'model-b']
    asyncio.run(check())
