"""QR tests isolate all third-party, database and session I/O."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.fixture
def qr_env(trading, monkeypatch):
    from alphapulse.api import jin10
    from alphapulse.calendar import jin10_auth
    from alphapulse.core.auth import AuthSession
    from alphapulse.core.crypto import encrypt
    from fastapi import FastAPI
    from alphapulse.core.db import get_session
    from alphapulse.core.auth import require_admin
    row = SimpleNamespace(id=1, token_encrypted=None, connected_at=None, task_id=None,
                          owner=None, qr_encrypted=None, expires_at=None, next_poll_at=None, error=None)
    session = SimpleNamespace(scalar=AsyncMock(return_value=row), commit=AsyncMock())
    principal = AuthSession("test", "session-a", 9999999999)
    app = FastAPI()
    app.include_router(jin10.router)
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(jin10, "check_session", AsyncMock())
    monkeypatch.setattr(jin10_auth, "generate_qr", AsyncMock(return_value="private-qr-token"))
    monkeypatch.setattr(jin10_auth, "poll_qr", AsyncMock(return_value=("pending", None)))
    def authorize():
        app.dependency_overrides[require_admin] = lambda: principal
    def pending():
        row.task_id, row.owner = "task-a", principal.jti
        row.qr_encrypted = encrypt("private-qr-token")
        row.expires_at = datetime.now(UTC) + timedelta(seconds=60)
        row.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)
    return SimpleNamespace(api=jin10, upstream=jin10_auth, app=app, row=row, session=session,
                           principal=principal, authorize=authorize, pending=pending)


def request(env, method, path="", **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(env.app), base_url="http://test") as client:
            return await client.request(method, "/api/settings/jin10" + path, **kwargs)
    return asyncio.run(run())


@pytest.mark.parametrize("method,path", [("GET", ""), ("POST", "/qr"), ("POST", "/qr/task-a/poll"),
                                         ("DELETE", "/qr/task-a"), ("DELETE", "")])
def test_auth_required(qr_env, method, path):
    assert request(qr_env, method, path).status_code == 401
    qr_env.session.scalar.assert_not_awaited()


def test_create_hides_credentials_and_invalidates_old_task(qr_env):
    from alphapulse.core.crypto import decrypt
    e = qr_env
    e.authorize()
    e.pending()
    response = request(e, "POST", "/qr")
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["image"].startswith("data:image/png;base64,")
    assert "private-qr-token" not in response.text
    assert e.row.task_id != "task-a"
    assert decrypt(e.row.qr_encrypted) == "private-qr-token"
    assert e.row.owner == e.principal.jti
    assert request(e, "POST", "/qr/task-a/poll").status_code == 404


@pytest.mark.parametrize("owner,expired", [("different-session", False), ("session-a", True)])
def test_owner_and_expiry_block_upstream(qr_env, owner, expired):
    e = qr_env
    e.authorize()
    e.pending()
    e.row.owner = owner
    if expired:
        e.row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    response = request(e, "POST", "/qr/task-a/poll")
    assert response.status_code == (200 if expired else 404)
    if expired:
        assert response.json()["data"]["state"] == "expired"
        assert e.row.qr_encrypted is None
    e.upstream.poll_qr.assert_not_awaited()


def test_poll_throttles_and_stores_success_encrypted(qr_env):
    from alphapulse.core.crypto import decrypt
    e = qr_env
    e.authorize()
    e.pending()
    e.row.next_poll_at = datetime.now(UTC) + timedelta(seconds=5)
    assert request(e, "POST", "/qr/task-a/poll").json()["data"]["state"] == "pending"
    e.upstream.poll_qr.assert_not_awaited()
    e.row.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)
    e.upstream.poll_qr.return_value = "connected", "private-account-token"
    response = request(e, "POST", "/qr/task-a/poll")
    assert response.json()["data"]["state"] == "connected"
    assert "private-account-token" not in response.text
    assert decrypt(e.row.token_encrypted) == "private-account-token"
    assert e.row.qr_encrypted is None
    status = request(e, "GET")
    assert status.json()["data"]["sync_state"] == "awaiting_validation"
    assert "private-account-token" not in status.text


@pytest.mark.parametrize("failure", [False, True])
def test_revoke_during_upstream_cannot_save_result(qr_env, failure):
    from fastapi import HTTPException
    e = qr_env
    e.authorize()
    e.pending()
    e.upstream.poll_qr.return_value = "connected", "private-account-token"
    if failure:
        e.upstream.poll_qr.side_effect = e.upstream.Jin10Error("upstream failed")
    e.api.check_session.side_effect = HTTPException(401, "expired")
    assert request(e, "POST", "/qr/task-a/poll").status_code == 401
    assert e.row.token_encrypted is None
    assert e.row.error is None
    assert e.session.commit.await_count == 1  # Only the poll claim was committed.


@pytest.mark.parametrize("action", ["cancel", "replace", "disconnect"])
@pytest.mark.parametrize("failure", [False, True])
def test_invalidated_poll_cannot_change_connection(qr_env, action, failure):
    e = qr_env
    e.authorize()
    e.pending()

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def upstream(token):
            entered.set()
            await release.wait()
            if failure:
                raise e.upstream.Jin10Error("upstream failed")
            return "connected", "private-account-token"

        e.upstream.poll_qr.side_effect = upstream
        async with httpx.AsyncClient(transport=httpx.ASGITransport(e.app), base_url="http://test") as client:
            polling = asyncio.create_task(client.post("/api/settings/jin10/qr/task-a/poll"))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                if action == "replace":
                    response = await client.post("/api/settings/jin10/qr")
                    assert response.status_code == 201
                    replacement = response.json()["data"]["task_id"]
                else:
                    path = "/qr/task-a" if action == "cancel" else ""
                    response = await client.delete("/api/settings/jin10" + path)
                    assert response.status_code == 200
                commits = e.session.commit.await_count
            finally:
                release.set()
            assert (await polling).status_code == 404
            assert e.session.commit.await_count == commits
            assert e.row.token_encrypted is None
            assert e.row.error is None
            assert e.row.task_id == (replacement if action == "replace" else None)

    asyncio.run(run())


def test_overlapping_polls_only_contact_upstream_once(qr_env):
    e = qr_env
    e.authorize()
    e.pending()

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def upstream(token):
            entered.set()
            await release.wait()
            return "pending", None

        e.upstream.poll_qr.side_effect = upstream
        async with httpx.AsyncClient(transport=httpx.ASGITransport(e.app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/settings/jin10/qr/task-a/poll"))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                duplicate = await client.post("/api/settings/jin10/qr/task-a/poll")
                assert duplicate.json()["data"]["state"] == "pending"
                assert e.upstream.poll_qr.await_count == 1
            finally:
                release.set()
            assert (await first).json()["data"]["state"] == "pending"
            e.upstream.poll_qr.return_value = "connected", "private-account-token"
            e.upstream.poll_qr.side_effect = None
            e.row.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)
            assert (await client.post("/api/settings/jin10/qr/task-a/poll")).json()["data"]["state"] == "connected"

    asyncio.run(run())


def test_disconnect_and_cancel_do_not_contact_remote_logout(qr_env):
    e = qr_env
    e.authorize()
    e.pending()
    assert request(e, "DELETE", "/qr/task-a").status_code == 200
    assert e.row.qr_encrypted is None
    e.row.token_encrypted = b"encrypted"
    assert request(e, "DELETE").status_code == 200
    assert e.row.token_encrypted is None
    e.upstream.poll_qr.assert_not_awaited()


@pytest.mark.parametrize("data,state", [(None, "pending"), ({}, "pending"), ({"qr_status": "timeout"}, "expired")])
def test_sdk_pending_and_expiry_responses(qr_env, monkeypatch, data, state):
    from alphapulse.calendar import jin10_auth
    # Restore the real parser, while keeping HTTP isolated.
    import importlib
    jin10_auth = importlib.reload(jin10_auth)
    monkeypatch.setattr(jin10_auth, "uc_request", AsyncMock(return_value={"status": 200, "data": data}))
    assert asyncio.run(jin10_auth.poll_qr("private")) == (state, None)


def test_upstream_errors_never_expose_tokens(qr_env, monkeypatch):
    from alphapulse.calendar import jin10_auth
    import importlib
    jin10_auth = importlib.reload(jin10_auth)
    mock = AsyncMock(side_effect=httpx.ConnectError("private-token-in-url"))
    monkeypatch.setattr(httpx.AsyncClient, "get", mock)
    with pytest.raises(jin10_auth.Jin10Error, match="金十连接失败") as exc:
        asyncio.run(jin10_auth.uc_request("/qrcode/try_login", {"token": "private"}))
    assert "private" not in str(exc.value)


@pytest.mark.parametrize("message,expired", [("二维码已过期", True), ("请求被拒绝", False)])
def test_observed_upstream_expiry_is_not_a_generic_connection_failure(qr_env, monkeypatch, message, expired):
    from alphapulse.calendar import jin10_auth
    import importlib
    jin10_auth = importlib.reload(jin10_auth)
    response = httpx.Response(200, json={"status": 400, "message": message}, request=httpx.Request("GET", "https://uc-api.jin10.com/qrcode/try_login"))
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=response))
    if expired:
        assert asyncio.run(jin10_auth.poll_qr("private")) == ("expired", None)
    else:
        with pytest.raises(jin10_auth.Jin10Error, match="未接受请求"):
            asyncio.run(jin10_auth.poll_qr("private"))


def test_upstream_expiry_clears_task_without_disconnecting_existing_account(qr_env):
    e = qr_env
    e.authorize()
    e.pending()
    e.row.token_encrypted = b"existing-encrypted-credential"
    e.upstream.poll_qr.return_value = "expired", None
    response = request(e, "POST", "/qr/task-a/poll")
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "expired"
    assert e.row.token_encrypted == b"existing-encrypted-credential"
    assert e.row.qr_encrypted is None
    assert e.row.error is None
