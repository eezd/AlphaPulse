from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

from alphapulse.api import auth, chat, crawler, market, news, settings, skills, trading
from alphapulse.core.auth import require_admin

# 关闭默认文档路由，改为登录后才能访问
app = FastAPI(title="AlphaPulse", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth.router)
app.include_router(news.router)
app.include_router(crawler.router)
app.include_router(market.router)
app.include_router(market.ws_router)
app.include_router(settings.router)
app.include_router(skills.router)
app.include_router(chat.router)
app.include_router(trading.router)


@app.get("/api/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin)])
async def openapi() -> dict:
    return app.openapi()


@app.get("/api/docs", include_in_schema=False, dependencies=[Depends(require_admin)])
async def docs() -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="AlphaPulse API")
