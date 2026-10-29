"""认证和公开接口契约；Redis/数据库/外部服务均隔离。"""
import asyncio
import base64
import io
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from redis.exceptions import ConnectionError
from starlette.testclient import WebSocketDenialResponse


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.offset = 0
        self.failed = False

    def now(self):
        if self.failed:
            raise ConnectionError("isolated test failure")
        return time.time() + self.offset

    async def set(self, key, value, *, ex=None, exat=None):
        now = self.now()
        self.values[key] = (value, exat if exat is not None else now + ex)

    async def get(self, key):
        now = self.now()
        value = self.values.get(key)
        if value is None:
            return None
        if value[1] <= now:
            self.values.pop(key)
            return None
        return value[0]

    async def getdel(self, key):
        value = await self.get(key)
        self.values.pop(key, None)
        return value

    async def delete(self, key):
        self.now()
        self.values.pop(key, None)

    async def ttl(self, key):
        return max(0, int(self.values[key][1] - self.now())) if await self.get(key) else -2

    async def eval(self, script, numkeys, key, seconds, threshold):
        current = int(await self.get(key) or 0) + 1
        expires = self.now() + seconds if current in (1, threshold) else self.values[key][1]
        self.values[key] = (str(current), expires)
        return current


@pytest.fixture
def auth_app(trading, monkeypatch):
    from alphapulse.api import auth
    from alphapulse.api.main import app
    from alphapulse.core import auth as core
    settings = core.get_settings()
    monkeypatch.setattr(settings, "jwt_secret", "isolated-auth-test-secret-at-least-32-bytes")
    monkeypatch.setattr(settings, "admin_username", "test-admin")
    monkeypatch.setattr(settings, "admin_password_hash", core._hasher.hash("test-password"))
    monkeypatch.setattr(settings, "login_captcha_enabled", True)
    memory = MemoryRedis()
    monkeypatch.setattr(auth, "redis", memory)
    monkeypatch.setattr(core, "redis", memory)
    yield app, memory
    app.dependency_overrides.clear()


async def challenge(client):
    result = await client.get("/api/auth/captcha")
    assert result.status_code == 200
    body = result.json()
    assert body["code"] == 0 and body["message"] == ""
    return body["data"]


async def login(client, memory, *, password="test-password"):
    from alphapulse.api.auth import captcha_digest
    from alphapulse.core.auth import AUTH_PREFIX
    data = await challenge(client)
    await memory.set(f"{AUTH_PREFIX}captcha:{data['captcha_id']}", captcha_digest(data["captcha_id"], "ABCDE"), ex=120)
    return await client.post("/api/auth/login", json={"username": "test-admin", "password": password,
                                                    "captcha_id": data["captcha_id"], "code": "ABCDE"})


def test_login_png_restore_revoke_current_session_only(auth_app):
    app, memory = auth_app
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            image = await challenge(client)
            with Image.open(io.BytesIO(base64.b64decode(image["image"].split(",")[1]))) as png:
                assert png.format == "PNG" and png.size == (180, 60)
            assert set(image) == {"enabled", "captcha_id", "image", "expires_in"}
            assert image["enabled"] is True
            first, second = await login(client, memory), await login(client, memory)
            assert first.status_code == second.status_code == 200
            token = first.json()["data"]
            assert token["token_type"] == "bearer" and token["expires_at"]
            assert "set-cookie" not in first.headers
            headers = {"Authorization": f"Bearer {token['token']}"}
            assert (await client.get("/api/auth/me", headers=headers)).json()["data"] == {"username": "test-admin", "is_admin": True}
            assert (await client.post("/api/auth/logout", headers=headers)).json() == {"code": 0, "data": None, "message": ""}
            denied = await client.get("/api/auth/me", headers=headers)
            assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
            assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {second.json()['data']['token']}"})).status_code == 200
    asyncio.run(check())


def test_password_login_with_captcha_disabled(auth_app, monkeypatch):
    from alphapulse.core.auth import get_settings
    from alphapulse.core.config import Settings
    app, memory = auth_app
    assert Settings.model_fields["login_captcha_enabled"].default is False
    monkeypatch.setattr(get_settings(), "login_captcha_enabled", False)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert await challenge(client) == {"enabled": False}
            assert memory.values == {}
            body = {"username": "test-admin", "password": "test-password"}
            signed = await client.post("/api/auth/login", json=body)
            assert signed.status_code == 200
            headers = {"Authorization": "Bearer " + signed.json()["data"]["token"]}
            assert (await client.get("/api/auth/me", headers=headers)).json()["data"]["is_admin"] is True
            assert (await client.post("/api/auth/logout", headers=headers)).status_code == 200
            assert (await client.get("/api/auth/me", headers=headers)).status_code == 401
            for _ in range(5):
                assert (await client.post("/api/auth/login", json={**body, "password": "wrong"})).status_code == 401
            locked = await client.post("/api/auth/login", json=body)
            assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
            memory.offset += 901
            assert (await client.post("/api/auth/login", json=body)).status_code == 200
            memory.failed = True
            assert (await client.post("/api/auth/login", json=body)).status_code == 503
    asyncio.run(check())


@pytest.mark.parametrize("fields", [{}, {"captcha_id": "challenge"}, {"code": "ABCDE"}])
def test_enabled_captcha_cannot_be_omitted(auth_app, fields):
    app, memory = auth_app
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/auth/login", json={"username": "test-admin", "password": "test-password", **fields})
            assert response.status_code == 400 and response.json()["code"] == 400
            assert not any("session:" in key for key in memory.values)
    asyncio.run(check())


@pytest.mark.parametrize("case", ["wrong", "expired", "replay", "concurrent"])
def test_captcha_is_bound_consumed_and_expires(auth_app, case):
    from alphapulse.api.auth import captcha_digest
    from alphapulse.core.auth import AUTH_PREFIX
    app, memory = auth_app
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            data = await challenge(client)
            key = f"{AUTH_PREFIX}captcha:{data['captcha_id']}"
            await memory.set(key, captcha_digest(data["captcha_id"], "ABCDE"), ex=120)
            body = {"username": "test-admin", "password": "test-password", "captcha_id": data["captcha_id"], "code": "ABCDE"}
            if case == "wrong":
                body["code"] = "XYZ23"
                assert (await client.post("/api/auth/login", json=body)).status_code == 400
                body["code"] = "ABCDE"
            if case == "expired":
                memory.offset += 121
            if case == "replay":
                assert (await client.post("/api/auth/login", json=body)).status_code == 200
            if case == "concurrent":
                results = await asyncio.gather(*(client.post("/api/auth/login", json=body) for _ in range(2)))
                assert sorted(result.status_code for result in results) == [200, 400]
            else:
                assert (await client.post("/api/auth/login", json=body)).status_code == 400
            assert await memory.get(key) is None
            body["captcha_id"] = "not-the-same-challenge"
            assert (await client.post("/api/auth/login", json=body)).status_code == 400
    asyncio.run(check())


def test_login_limits_and_challenge_limits(auth_app):
    app, memory = auth_app
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            for attempt in range(5):
                if attempt: memory.offset += 100
                assert (await login(client, memory, password="wrong")).status_code == 401
            result = await login(client, memory)
            assert result.status_code == 429 and int(result.headers["retry-after"]) >= 899
            memory.offset += 901
            assert (await login(client, memory)).status_code == 200
            memory.offset += 61
            for _ in range(30):
                assert (await client.get("/api/auth/captcha")).status_code == 200
            limited = await client.get("/api/auth/captcha")
            assert limited.status_code == 429 and limited.headers["retry-after"]
    asyncio.run(check())


@pytest.mark.parametrize("case", ["cookie", "legacy", "expired", "forbidden", "redis"])
def test_auth_failure_boundaries(auth_app, case):
    from alphapulse.core import auth
    app, memory = auth_app
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            token, _ = await auth.create_token("former-admin" if case == "forbidden" else "test-admin")
            if case == "legacy":
                token = jwt.encode({"sub": "test-admin", "exp": int(time.time()) + 60}, auth.get_settings().jwt_secret, algorithm="HS256")
            if case == "expired":
                payload = jwt.decode(token, auth.get_settings().jwt_secret, algorithms=["HS256"])
                payload["exp"] = int(time.time()) - 1
                token = jwt.encode(payload, auth.get_settings().jwt_secret, algorithm="HS256")
            headers = {"Authorization": f"Bearer {token}"} if case != "cookie" else {"Cookie": f"ap_token={token}"}
            memory.failed = case == "redis"
            result = await client.get("/api/news", headers=headers)
            assert result.status_code == (403 if case == "forbidden" else 503 if case == "redis" else 401)
            assert result.json()["code"] == result.status_code
            if case == "forbidden":
                assert (await client.get("/api/auth/me", headers=headers)).json()["data"]["is_admin"] is False
            if case == "redis":
                assert (await client.get("/api/auth/captcha")).status_code == 503
    asyncio.run(check())


def test_protocol_status_validation_redaction_openapi(auth_app, monkeypatch):
    from alphapulse.api import market
    from alphapulse.core.auth import create_token
    from alphapulse.core.db import get_session
    app, memory = auth_app
    app.dependency_overrides[get_session] = lambda: SimpleNamespace(add=lambda row: None, commit=AsyncMock())
    monkeypatch.setattr(market, "tradable_symbols", AsyncMock(return_value=[{"symbol": "BTCUSDT", "base": "BTC", "quote": "USDT", "price_precision": 2, "volume_precision": 5}]))
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/api/health")).json() == {
                "code": 0, "data": {"status": "ok", "version": "source", "commit": "unknown"}, "message": "",
            }
            monkeypatch.setenv("APP_VERSION", "20261029-aaaaaaaa")
            monkeypatch.setenv("APP_COMMIT", "a" * 40)
            assert (await client.get("/api/health")).json()["data"] == {
                "status": "ok", "version": "20261029-aaaaaaaa", "commit": "a" * 40,
            }
            invalid = await client.post("/api/auth/login", json={"password": {"sensitive": "do-not-echo"}})
            assert invalid.status_code == 422 and invalid.json()["code"] == 422
            assert "do-not-echo" not in invalid.text and "input" not in invalid.text
            assert all(set(item) == {"loc", "type", "msg"} for item in invalid.json()["data"]["errors"])
            assert (await client.get("/api/unknown")).json()["code"] == 404
            token, _ = await create_token("test-admin")
            client.headers["Authorization"] = f"Bearer {token}"
            response = await client.post("/api/market/symbols", json={"market": "spot", "symbol": "BTCUSDT"})
            assert response.status_code == 201 and response.json() == {"code": 0, "data": None, "message": ""}
            schema = await client.get("/api/openapi.json")
            assert "openapi" in schema.json() and "code" not in schema.json()
            validation_schema = schema.json()["paths"]["/api/auth/login"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]
            assert "ApiResponse_ValidationErrors_" in validation_schema["$ref"]
            assert schema.json()["paths"]["/api/auth/me"]["get"]["security"] == [{"HTTPBearer": []}]
            assert (await client.get("/api/docs")).headers["location"] == "/docs"
            monkeypatch.setattr(market, "tradable_symbols", AsyncMock(side_effect=RuntimeError("private provider failure")))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://test",
                                         headers={"Authorization": f"Bearer {token}"}) as failing:
                failed = await failing.post("/api/market/symbols", json={"market": "spot", "symbol": "BTCUSDT"})
                assert failed.status_code == 500
                assert failed.json() == {"code": 500, "data": None, "message": "服务器内部错误"}
                assert "private provider failure" not in failed.text

    asyncio.run(check())


def test_stream_revoke_closes_producer_and_redis_failure_is_not_401(auth_app):
    from alphapulse.core.auth import AuthSession, create_token, revoke_session
    from alphapulse.core.streams import session_events
    from fastapi.sse import ServerSentEvent
    app, memory = auth_app
    async def check():
        closed = []
        async def source():
            try:
                yield ServerSentEvent(data={"value": 1}, event="news")
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        async def principal():
            token, _ = await create_token("test-admin")
            payload = jwt.decode(token, options={"verify_signature": False})
            return AuthSession(payload["sub"], payload["jti"], payload["exp"])
        session = await principal()
        stream = session_events(source(), session)
        assert (await anext(stream)).event == "news"
        await revoke_session(session)
        event = await asyncio.wait_for(anext(stream), timeout=2)
        assert event.event == "auth_error" and event.data["code"] == 401
        await stream.aclose()
        assert closed == [True]
        session = await principal()
        memory.failed = True
        stream = session_events(source(), session)
        event = await anext(stream)
        assert event.event == "error" and event.data["code"] == 503
        await stream.aclose()
    asyncio.run(check())


def test_websocket_tickets_single_use_active_session(auth_app, monkeypatch):
    from alphapulse.api import market
    from alphapulse.core.auth import WS_PROTOCOL, create_token
    app, memory = auth_app
    monkeypatch.setattr(market.redis, "pubsub", lambda: SimpleNamespace(aclose=AsyncMock()))
    token, _ = asyncio.run(create_token("test-admin"))
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        expired = client.post("/api/auth/ws-ticket").json()["data"]["ticket"]
        memory.offset += 31
        with pytest.raises(WebSocketDenialResponse) as expired_response:
            with client.websocket_connect("/api/market/ws", subprotocols=[WS_PROTOCOL, f"ticket.{expired}"]):
                pass
        assert expired_response.value.status_code == 401
        ticket = client.post("/api/auth/ws-ticket").json()["data"]["ticket"]
        unused = client.post("/api/auth/ws-ticket").json()["data"]["ticket"]
        protocols = [WS_PROTOCOL, f"ticket.{ticket}"]
        with client.websocket_connect("/api/market/ws", subprotocols=protocols) as socket:
            assert socket.accepted_subprotocol == WS_PROTOCOL
            client.post("/api/auth/logout")
            assert socket.receive()["code"] == 4401
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect("/api/market/ws", subprotocols=protocols):
                pass
        assert denied.value.status_code == 401
        with pytest.raises(WebSocketDenialResponse) as revoked:
            with client.websocket_connect("/api/market/ws", subprotocols=[WS_PROTOCOL, f"ticket.{unused}"]):
                pass
        assert revoked.value.status_code == 401


def test_trading_config_returns_single_envelope_and_manual_request_keeps_202(auth_app, monkeypatch):
    from alphapulse.api import trading
    from alphapulse.core.auth import create_token
    from alphapulse.core.db import get_session
    from alphapulse.models import TradingAccount
    app, memory = auth_app
    account = SimpleNamespace(exchange="bybit", provider_id=None, reasoning_effort=None, daily_analysis_limit=60)
    async def get(model, key):
        return account if model is TradingAccount else None
    db = SimpleNamespace(get=get, add=lambda row: None, commit=AsyncMock(),
                         execute=AsyncMock(return_value=SimpleNamespace(one=lambda: (2, 10, 5))))
    app.dependency_overrides[get_session] = lambda: db
    monkeypatch.setattr(trading, "read_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(trading, "get_symbol", AsyncMock(return_value=SimpleNamespace(running=True)))
    monkeypatch.setattr(trading, "fresh_snapshot", AsyncMock())
    monkeypatch.setattr(trading, "active_exchange", AsyncMock(return_value="bybit"))
    async def check():
        token, _ = await create_token("test-admin")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            result = await client.patch("/api/trading/config", json={"daily_analysis_limit": 120})
            assert result.status_code == 200
            assert result.json()["data"]["today"]["limit"] == 120
            assert "code" not in result.json()["data"]
            result = await client.post("/api/trading/symbols/BTCUSDT/manual/cancel")
            assert result.status_code == 202
            assert result.json() == {"code": 0, "data": None, "message": ""}
    asyncio.run(check())
