"""Use Jin10's own browser SDK rather than reproducing its encrypted transport."""
import asyncio
from datetime import date

from playwright.async_api import Error as BrowserError, async_playwright

from alphapulse.calendar.jin10 import KINDS, day_url, normalize
from alphapulse.calendar.jin10_auth import Jin10Error, uc_request

METHODS = {"data": "getCalendarData", "event": "getCalendarEvent", "holiday": "getCalendarHoliday"}
VALIDATION_DAY = date(2025, 1, 8)


def validate_access(responses: dict) -> None:
    # This archived day has verified economic releases and events. An authenticated
    # account alone does not prove that the SDK can read the economic calendar.
    normalized = {kind: normalize(responses[kind], kind, VALIDATION_DAY) for kind in KINDS}
    if not normalized["data"] or not normalized["event"]:
        raise Jin10Error("金十历史日历读取权限未通过验证，请重新连接后检查账号权限")


class Jin10Browser:
    def __init__(self) -> None:
        self.playwright = self.browser = self.context = self.page = None
        self.credential = None
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.playwright = self.browser = self.context = self.page = None
        self.credential = None

    async def connect(self, credential: str) -> None:
        await self.close()
        info = await uc_request("/userinfo", token=credential)
        if not isinstance(info.get("data"), dict) or not info["data"]:
            raise Jin10Error("金十登录态已失效，请重新扫码")
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            self.context = await self.browser.new_context()
            await self.context.add_cookies([{"name": "x-token", "value": credential, "domain": ".jin10.com",
                                            "path": "/", "secure": True, "sameSite": "Lax"}])
            self.page = await self.context.new_page()
            # Keep the host page outside the collection window so its initial requests do not compete.
            await self.page.goto(day_url(date(2024, 1, 1)), wait_until="domcontentloaded", timeout=30000)
            await self.page.wait_for_function(
                "window.Jin10FlashInstance && window.Jin10FlashInstance.socket && "
                "window.Jin10FlashInstance.socket.readyState === 1", timeout=30000)
            await self.page.wait_for_timeout(2000)
            self.credential = credential
            validate_access(await self.fetch_day(VALIDATION_DAY))
        except BrowserError:
            await self.close()
            raise Jin10Error("金十浏览器连接失败，请检查网络与浏览器运行环境") from None

    async def fetch_day(self, day: date) -> dict[str, dict]:
        async with self.lock:
            if self.page is None:
                raise Jin10Error("金十浏览器尚未连接")
            try:
                result = {}
                for kind in KINDS:
                    result[kind] = await self.page.evaluate(
                        "async ({method, day}) => {"
                        "if (!window.Jin10FlashInstance || window.Jin10FlashInstance.socket.readyState !== 1) throw Error('disconnected');"
                        "return await window.Jin10FlashInstance[method](day, {timeout: 15000});}",
                        {"method": METHODS[kind], "day": day.isoformat()})
                info = await uc_request("/userinfo", token=self.credential)
                if not isinstance(info.get("data"), dict) or not info["data"]:
                    raise Jin10Error("金十登录态已失效，未将空列表记为完整日历")
                return result
            except BrowserError:
                # Browser exceptions may include URLs or script contents; never forward them to logs.
                raise Jin10Error("金十日历请求失败或超时，未将本次响应记为成功") from None
