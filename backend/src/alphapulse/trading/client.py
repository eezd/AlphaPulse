"""币安 U 本位合约模拟盘（Demo Trading）签名客户端

只连模拟盘：地址写死，没有切换实盘的参数，避免误下真单。
"""

import hashlib
import hmac
import re
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from alphapulse.trading.plan import SymbolRules

BASE_URL = "https://demo-fapi.binance.com"
RECV_WINDOW_MS = 5000
TIMEOUT_SECONDS = 10
EXCHANGE_INFO_SECONDS = 3600
# 本程序下的单 clientOrderId / clientAlgoId 为 ap{流水 id}；其他（含用户手动下的）不动
OWN_CLIENT_ID = re.compile(r"^ap\d+$")


class ExchangeError(Exception):
    """交易所或网络错误；code 为币安错误码（如 -2021），网络层错误时 status、code 为空"""

    def __init__(self, status: int | None, code: int | None, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        # 只有限流、服务端错误、-1001（内部断连）可以重试；业务错误重试也不会成功
        return self.status in (418, 429) or (self.status or 0) >= 500 or self.code == -1001

    def __str__(self) -> str:
        if self.status is None:
            return self.message
        return f"币安返回 {self.status}（{self.code}）：{self.message}"


async def public_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """模拟盘公开接口（exchangeInfo 等），不需要 key"""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_SECONDS) as http:
        try:
            resp = await http.get(path, params=params)
        except httpx.TransportError as e:
            raise ExchangeError(None, None, f"无法连接币安模拟盘：{e!r}") from e
    if resp.status_code >= 400:
        raise ExchangeError(resp.status_code, None, resp.text[:200])
    return resp.json()


class DemoClient:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self.secret = api_secret.encode()
        self.http = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_SECONDS, headers={"X-MBX-APIKEY": api_key})
        # 本地时钟与服务器的偏差（毫秒），遇到 -1021 时校准
        self.time_offset = 0

    async def __aenter__(self) -> "DemoClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.http.aclose()

    async def sync_time(self) -> None:
        server_time = (await self.request("GET", "/fapi/v1/time", signed=False))["serverTime"]
        self.time_offset = server_time - int(time.time() * 1000)

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = True) -> Any:
        """不自动重试：下单类请求重发可能重复成交，由调用方按 clientOrderId 查询后决定"""
        for attempt in range(2):
            # 币安要求布尔参数为小写字符串
            query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in (params or {}).items() if v is not None}
            if signed:
                query["timestamp"] = int(time.time() * 1000) + self.time_offset
                query["recvWindow"] = RECV_WINDOW_MS
            encoded = urlencode(query)
            if signed:
                encoded += "&signature=" + hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
            try:
                resp = await self.http.request(method, f"{path}?{encoded}" if encoded else path)
            except httpx.TransportError as e:
                raise ExchangeError(None, None, f"无法连接币安模拟盘：{e!r}") from e
            if resp.status_code < 400:
                return resp.json()
            try:
                body = resp.json()
            except ValueError:
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            # 时间戳超出接收窗口时请求未被受理，校准时钟后重发是安全的
            if code == -1021 and attempt == 0:
                await self.sync_time()
                continue
            raise ExchangeError(resp.status_code, code, body.get("msg", resp.text[:200]) if isinstance(body, dict) else resp.text[:200])
        raise AssertionError("unreachable")


_exchange_info: tuple[float, dict[str, SymbolRules]] | None = None


def step_decimal(raw: str) -> Decimal:
    """去掉多余的 0（"0.10" → 0.1），但整数步长不能变成 1E+2 这类指数形式，否则按它取整的价格和数量也会是指数形式"""
    value = Decimal(raw).normalize()
    return value.quantize(Decimal(1)) if value.as_tuple().exponent > 0 else value


async def symbol_rules(symbol: str) -> SymbolRules:
    """模拟盘的价格精度、数量精度、最小下单金额，按小时缓存"""
    global _exchange_info
    if _exchange_info is None or time.monotonic() - _exchange_info[0] > EXCHANGE_INFO_SECONDS:
        info = await public_get("/fapi/v1/exchangeInfo")
        parsed = {}
        for s in info["symbols"]:
            filters = {f["filterType"]: f for f in s["filters"]}
            parsed[s["symbol"]] = SymbolRules(
                tick_size=step_decimal(filters["PRICE_FILTER"]["tickSize"]),
                step_size=step_decimal(filters["LOT_SIZE"]["stepSize"]),
                min_notional=Decimal(filters["MIN_NOTIONAL"]["notional"]),
            )
        _exchange_info = (time.monotonic(), parsed)
    if symbol not in _exchange_info[1]:
        raise ExchangeError(None, None, f"模拟盘没有交易对 {symbol}")
    return _exchange_info[1][symbol]


async def mark_price(symbol: str) -> Decimal:
    return Decimal((await public_get("/fapi/v1/premiumIndex", {"symbol": symbol}))["markPrice"])
