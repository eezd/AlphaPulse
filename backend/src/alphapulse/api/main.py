import os

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException

from alphapulse.api import auth, calendar, chat, crawler, market, news, settings, skills, trading
from alphapulse.api.response import (
    ApiResponse, ValidationErrors, http_error, internal_error, success, validation_error,
)
from alphapulse.core.auth import require_admin

# 关闭默认文档路由，改为登录后才能访问
app = FastAPI(
    title="AlphaPulse", docs_url=None, redoc_url=None, openapi_url=None,
    responses={422: {"model": ApiResponse[ValidationErrors], "description": "请求参数无效"}},
)
app.add_exception_handler(HTTPException, http_error)
app.add_exception_handler(RequestValidationError, validation_error)
app.add_exception_handler(Exception, internal_error)
app.include_router(auth.router)
app.include_router(news.router)
app.include_router(crawler.router)
app.include_router(market.router)
app.include_router(market.ws_router)
app.include_router(settings.router)
app.include_router(skills.router)
app.include_router(chat.router)
app.include_router(trading.router)
app.include_router(calendar.router)


@app.get("/api/health", include_in_schema=False)
async def health() -> ApiResponse[dict[str, str]]:
    return success({"status": "ok", "version": os.environ.get("APP_VERSION", "source"),
                    "commit": os.environ.get("APP_COMMIT", "unknown")})


@app.get("/api/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin)])
async def openapi() -> dict:
    return app.openapi()


@app.get("/api/docs", include_in_schema=False)
async def docs() -> RedirectResponse:
    return RedirectResponse("/docs")
