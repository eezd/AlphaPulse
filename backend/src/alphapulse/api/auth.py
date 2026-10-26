import base64
import hashlib
import hmac
import io
import secrets
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from alphapulse.api.response import ApiResponse, success
from alphapulse.core.auth import (
    AUTH_PREFIX, AuthSession, create_token, create_ws_ticket, require_admin,
    require_session, revoke_session, unavailable, verify_password,
)
from alphapulse.core.config import get_settings
from alphapulse.core.redis import redis

router = APIRouter(prefix="/api/auth", tags=["auth"])
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
CAPTCHA_SECONDS = 120
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# INCR 与首次设置过期必须是同一个原子操作。
COUNT_SCRIPT = (
    "local n=redis.call('INCR',KEYS[1]); "
    "if n==1 or n==tonumber(ARGV[2]) then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n"
)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    captcha_id: str | None = Field(default=None, min_length=1, max_length=128)
    code: str | None = Field(default=None, min_length=1, max_length=32)


class TokenOut(BaseModel):
    token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


class MeOut(BaseModel):
    username: str
    is_admin: bool


class CaptchaOut(BaseModel):
    enabled: Literal[True] = True
    captcha_id: str
    image: str
    expires_in: int = CAPTCHA_SECONDS


class CaptchaDisabled(BaseModel):
    enabled: Literal[False] = False


class TicketOut(BaseModel):
    ticket: str
    expires_in: int = 30


def captcha_digest(captcha_id: str, code: str) -> str:
    return hmac.new(get_settings().jwt_secret.encode(), f"{captcha_id}:{code.upper()}".encode(), hashlib.sha256).hexdigest()


def captcha_image(code: str) -> str:
    image = Image.new("RGB", (180, 60), "#f1f5f9")
    draw = ImageDraw.Draw(image)
    for _ in range(8):
        draw.line([(secrets.randbelow(180), secrets.randbelow(60)) for _ in range(2)], fill="#94a3b8", width=1)
    font = ImageFont.load_default(size=32)
    for index, letter in enumerate(code):
        glyph = Image.new("RGBA", (36, 48))
        ImageDraw.Draw(glyph).text((4, 3), letter, font=font, fill="#0f172a")
        glyph = glyph.rotate(secrets.randbelow(25) - 12, resample=Image.Resampling.BICUBIC)
        image.paste(glyph, (index * 34 + 5, secrets.randbelow(7) + 3), glyph)
    for _ in range(100):
        draw.point((secrets.randbelow(180), secrets.randbelow(60)), fill="#64748b")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def too_many(seconds: int) -> HTTPException:
    return HTTPException(429, "请求过于频繁，请稍后再试", headers={"Retry-After": str(max(seconds, 1))})


@router.get("/captcha")
async def captcha(request: Request) -> ApiResponse[CaptchaOut | CaptchaDisabled]:
    if not get_settings().login_captcha_enabled:
        return success(CaptchaDisabled())
    key = f"{AUTH_PREFIX}captcha-rate:{request.client.host}"
    try:
        if await redis.eval(COUNT_SCRIPT, 1, key, 60, 0) > 30:
            raise too_many(await redis.ttl(key))
        captcha_id = secrets.token_urlsafe(32)
        code = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(5))
        await redis.set(f"{AUTH_PREFIX}captcha:{captcha_id}", captcha_digest(captcha_id, code), ex=CAPTCHA_SECONDS)
    except RedisError as error:
        raise unavailable() from error
    return success(CaptchaOut(captcha_id=captcha_id, image=captcha_image(code)))


@router.post("/login")
async def login(body: LoginIn, request: Request) -> ApiResponse[TokenOut]:
    settings = get_settings()
    if not settings.admin_password_hash:
        raise HTTPException(503, "管理员密码未配置")
    fail_key = f"{AUTH_PREFIX}login-fail:{request.client.host}"
    try:
        if int(await redis.get(fail_key) or 0) >= MAX_FAILURES:
            raise too_many(await redis.ttl(fail_key))
        if settings.login_captcha_enabled:
            digest = await redis.getdel(f"{AUTH_PREFIX}captcha:{body.captcha_id}") if body.captcha_id else None
            valid_captcha = digest is not None and body.code is not None and hmac.compare_digest(digest, captcha_digest(body.captcha_id, body.code))
            if not valid_captcha:
                await redis.eval(COUNT_SCRIPT, 1, fail_key, LOCK_SECONDS, MAX_FAILURES)
                raise HTTPException(400, "验证码错误、已过期或已使用，请刷新")
        if body.username != settings.admin_username or not verify_password(body.password, settings.admin_password_hash):
            await redis.eval(COUNT_SCRIPT, 1, fail_key, LOCK_SECONDS, MAX_FAILURES)
            raise HTTPException(401, "用户名或密码错误", headers={"WWW-Authenticate": "Bearer"})
        token, expires_at = await create_token(body.username)
        await redis.delete(fail_key)
    except RedisError as error:
        raise unavailable() from error
    return success(TokenOut(token=token, expires_at=expires_at))


@router.post("/logout")
async def logout(session: Annotated[AuthSession, Depends(require_session)]) -> ApiResponse[None]:
    await revoke_session(session)
    return success(None)


@router.get("/me")
async def me(session: Annotated[AuthSession, Depends(require_session)]) -> ApiResponse[MeOut]:
    return success(MeOut(username=session.username, is_admin=session.username == get_settings().admin_username))


@router.post("/ws-ticket")
async def ws_ticket(session: Annotated[AuthSession, Depends(require_admin)]) -> ApiResponse[TicketOut]:
    return success(TicketOut(ticket=await create_ws_ticket(session)))
