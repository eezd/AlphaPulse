import getpass
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError

from alphapulse.core.config import get_settings
from alphapulse.core.redis import KEY_PREFIX, redis

AUTH_PREFIX = f"{KEY_PREFIX}auth:"
WS_PROTOCOL = "alphapulse.market.v1"
_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False, bearerFormat="JWT")


@dataclass(frozen=True)
class AuthSession:
    username: str
    jti: str
    expires_at: int


def unauthorized() -> HTTPException:
    return HTTPException(401, "未登录或登录已过期", headers={"WWW-Authenticate": "Bearer"})


def unavailable() -> HTTPException:
    return HTTPException(503, "认证服务暂不可用")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


async def create_token(username: str) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(UTC)
    expire = now + timedelta(days=settings.jwt_expire_days)
    jti = secrets.token_urlsafe(32)
    token = jwt.encode({"sub": username, "iat": now, "exp": expire, "jti": jti},
                       settings.jwt_secret, algorithm="HS256")
    try:
        await redis.set(f"{AUTH_PREFIX}session:{jti}", username, exat=int(expire.timestamp()))
    except RedisError as error:
        raise unavailable() from error
    return token, expire


async def check_session(session: AuthSession) -> None:
    try:
        active = await redis.get(f"{AUTH_PREFIX}session:{session.jti}")
    except RedisError as error:
        raise unavailable() from error
    if session.expires_at <= datetime.now(UTC).timestamp() or active != session.username:
        raise unauthorized()


async def require_session(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> AuthSession:
    if credentials is None:
        raise unauthorized()
    token = credentials.credentials
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"],
                             options={"require": ["sub", "iat", "exp", "jti"]})
        if not isinstance(payload["jti"], str) or not payload["jti"]:
            raise jwt.InvalidTokenError("缺少会话标识")
        session = AuthSession(payload["sub"], payload["jti"], payload["exp"])
    except jwt.InvalidTokenError as error:
        raise unauthorized() from error
    await check_session(session)
    return session


async def require_admin(session: Annotated[AuthSession, Depends(require_session)]) -> AuthSession:
    if session.username != get_settings().admin_username:
        raise HTTPException(403, "没有管理员权限")
    return session


async def revoke_session(session: AuthSession) -> None:
    try:
        await redis.delete(f"{AUTH_PREFIX}session:{session.jti}")
    except RedisError as error:
        raise unavailable() from error


async def create_ws_ticket(session: AuthSession) -> str:
    ticket = secrets.token_urlsafe(32)
    try:
        await redis.set(f"{AUTH_PREFIX}ticket:{ticket}", json.dumps(session.__dict__), ex=30)
    except RedisError as error:
        raise unavailable() from error
    return ticket


async def websocket_admin(websocket: WebSocket) -> AuthSession:
    protocols = websocket.scope.get("subprotocols", [])
    tickets = [value[7:] for value in protocols if value.startswith("ticket.")]
    if WS_PROTOCOL not in protocols or len(tickets) != 1:
        raise unauthorized()
    try:
        raw = await redis.getdel(f"{AUTH_PREFIX}ticket:{tickets[0]}")
    except RedisError as error:
        raise unavailable() from error
    if raw is None:
        raise unauthorized()
    session = AuthSession(**json.loads(raw))
    await check_session(session)
    if session.username != get_settings().admin_username:
        raise HTTPException(403, "没有管理员权限")
    return session


if __name__ == "__main__":
    # 用法：python -m alphapulse.core.auth hash-password，输出写入 .env 的 ADMIN_PASSWORD_HASH
    if sys.argv[1:] != ["hash-password"]:
        sys.exit("用法：python -m alphapulse.core.auth hash-password")
    password = getpass.getpass("新密码：")
    if password != getpass.getpass("再次输入："):
        sys.exit("两次输入不一致")
    print(_hasher.hash(password))
