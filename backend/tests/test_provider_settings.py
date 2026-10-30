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
    reference = SimpleNamespace(enabled=enabled, provider_id=1)
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
