from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from alphapulse.core.auth import COOKIE_NAME, create_token, require_admin, verify_password
from alphapulse.core.config import get_settings
from alphapulse.core.redis import KEY_PREFIX, redis

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


class LoginIn(BaseModel):
    username: str
    password: str


class MeOut(BaseModel):
    username: str


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> MeOut:
    settings = get_settings()
    if not settings.admin_password_hash:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "管理员密码未配置")
    fail_key = f"{KEY_PREFIX}login:fail:{request.client.host}"
    if int(await redis.get(fail_key) or 0) >= MAX_FAILURES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "失败次数过多，请 15 分钟后再试")

    if body.username != settings.admin_username or not verify_password(body.password, settings.admin_password_hash):
        async with redis.pipeline() as pipe:
            await pipe.incr(fail_key).expire(fail_key, LOCK_SECONDS).execute()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

    await redis.delete(fail_key)
    response.set_cookie(
        COOKIE_NAME,
        create_token(body.username),
        max_age=settings.jwt_expire_days * 86400,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return MeOut(username=body.username)


@router.post("/logout")
async def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=True, samesite="strict")


@router.get("/me")
async def me(username: Annotated[str, Depends(require_admin)]) -> MeOut:
    return MeOut(username=username)
