"""Use the QR endpoints published by Jin10's current login component."""
import base64
from io import BytesIO

import httpx
import qrcode

UC_URL = "https://uc-api.jin10.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://rili.jin10.com/",
           "x-app-id": "YDcStOzjliuTJUvh", "x-version": "1.0.0"}


class Jin10Error(Exception):
    pass


class Jin10QrExpired(Jin10Error):
    pass


async def uc_request(path: str, params: dict | None = None, token: str | None = None) -> dict:
    headers = {**HEADERS, **({"x-token": token} if token else {})}
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(UC_URL + path, params=params, headers=headers)
            response.raise_for_status()
            body = response.json()
        if path == "/qrcode/try_login" and isinstance(body, dict) and body.get("status") == 400 and body.get("message") == "二维码已过期":
            raise Jin10QrExpired("金十二维码已过期")
        if not isinstance(body, dict) or body.get("status") != 200:
            raise Jin10Error("金十未接受请求，请重新扫码或稍后重试")
        return body
    except (httpx.HTTPError, ValueError) as exc:
        # Upstream messages and request URLs may contain credentials.
        raise Jin10Error("金十连接失败，请稍后重试") from exc


async def generate_qr() -> str:
    body = await uc_request("/qrcode/generate", {"device_type": "web"})
    value = body.get("data")
    if isinstance(value, dict):
        value = value.get("token")
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise Jin10Error("金十二维码响应格式已变化")
    return value


def qr_image(token: str) -> str:
    from urllib.parse import urlencode
    image = qrcode.make("https://www.jin10.com/?" + urlencode({"action": "login", "tag": token}))
    stream = BytesIO()
    image.save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


async def poll_qr(token: str) -> tuple[str, str | None]:
    try:
        body = await uc_request("/qrcode/try_login", {"token": token})
    except Jin10QrExpired:
        return "expired", None
    data = body.get("data")
    if data is None:
        return "pending", None
    if not isinstance(data, dict):
        raise Jin10Error("金十扫码状态响应格式已变化")
    credential = data.get("token")
    if isinstance(credential, str) and credential:
        # Confirm that a QR success really yields an authenticated account.
        info = await uc_request("/userinfo", token=credential)
        if not isinstance(info.get("data"), dict) or not info["data"]:
            raise Jin10Error("金十登录态验证失败")
        return "connected", credential
    if str(data.get("qr_status", "")).lower() in ("expired", "invalid", "timeout"):
        return "expired", None
    return "pending", None
