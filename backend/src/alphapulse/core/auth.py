import getpass
import sys
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, WebSocket, status

from alphapulse.core.config import get_settings

COOKIE_NAME = "ap_token"
_hasher = PasswordHasher()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def create_token(username: str) -> str:
    settings = get_settings()
    expire = datetime.now(UTC) + timedelta(days=settings.jwt_expire_days)
    return jwt.encode({"sub": username, "exp": expire}, settings.jwt_secret, algorithm="HS256")


def _username_from_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    return payload["sub"]


def require_admin(request: Request) -> str:
    username = _username_from_token(request.cookies.get(COOKIE_NAME))
    if username != get_settings().admin_username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或登录已过期")
    return username


def websocket_admin(websocket: WebSocket) -> str | None:
    # WebSocket 握手阶段不能用 HTTPException，由调用方自行关闭连接
    username = _username_from_token(websocket.cookies.get(COOKIE_NAME))
    return username if username == get_settings().admin_username else None


if __name__ == "__main__":
    # 用法：python -m alphapulse.core.auth hash-password，输出写入 .env 的 ADMIN_PASSWORD_HASH
    if sys.argv[1:] != ["hash-password"]:
        sys.exit("用法：python -m alphapulse.core.auth hash-password")
    password = getpass.getpass("新密码：")
    if password != getpass.getpass("再次输入："):
        sys.exit("两次输入不一致")
    print(_hasher.hash(password))
