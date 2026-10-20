"""Bybit USDT 永续模拟盘适配；交易地址固定为 Demo Trading，不能切换实盘。"""

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from alphapulse.trading.client import EXCHANGE_INFO_SECONDS, OWN_CLIENT_ID, RECV_WINDOW_MS, TIMEOUT_SECONDS, ExchangeError, step_decimal
from alphapulse.trading.plan import OrderSpec, SymbolRules

BASE_URL = "https://api-demo.bybit.com"
STATUSES = {
    "New": "NEW", "PartiallyFilled": "PARTIALLY_FILLED", "Filled": "FILLED",
    "Cancelled": "CANCELED", "PartiallyFilledCanceled": "CANCELED", "Rejected": "REJECTED",
    "Untriggered": "NEW", "Triggered": "NEW", "Deactivated": "CANCELED",
}


def response_result(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise ExchangeError(response.status_code, None, "Bybit 返回了无法解析的响应") from None
    code = body.get("retCode")
    if response.status_code >= 400 or code != 0:
        raise ExchangeError(response.status_code, code, body.get("retMsg", "Bybit 请求失败"))
    return body["result"]


async def public_get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_SECONDS) as http:
        try:
            response = await http.get(path, params=params)
        except httpx.TransportError as e:
            raise ExchangeError(None, None, "无法连接 Bybit 模拟盘") from e
    return response_result(response)


def normalize_order(row: dict, is_algo: bool = False) -> dict:
    status = STATUSES[row["orderStatus"]]
    result = {
        "symbol": row["symbol"], "orderId": row["orderId"], "clientOrderId": row["orderLinkId"] if OWN_CLIENT_ID.fullmatch(row["orderLinkId"]) else row["orderId"],
        "side": row["side"].upper(), "type": row["orderType"].upper(), "status": status,
        "price": row["price"] or "0", "origQty": row["qty"], "executedQty": row["cumExecQty"],
        "avgPrice": row["avgPrice"] or "0", "rejectReason": row.get("rejectReason"), "reduceOnly": row["reduceOnly"], "time": int(row["createdTime"]),
    }
    if is_algo:
        stop_type = row["stopOrderType"]
        if stop_type in ("StopLoss", "PartialStopLoss"):
            order_type = "STOP"
        elif stop_type in ("TakeProfit", "PartialTakeProfit"):
            order_type = "TAKE_PROFIT"
        elif stop_type == "Stop":
            order_type = "STOP" if (row["side"] == "Sell") == (row["triggerDirection"] == 2) else "TAKE_PROFIT"
        elif stop_type == "TrailingStop":
            order_type = "TRAILING_STOP"
        else:
            raise ExchangeError(None, None, f"Bybit 返回了不支持的条件单类型：{stop_type}")
        if row["orderType"] == "Market":
            order_type += "_MARKET"
        result |= {
            "algoId": row["orderId"], "clientAlgoId": result["clientOrderId"],
            "algoStatus": {"NEW": "NEW", "PARTIALLY_FILLED": "TRIGGERING", "FILLED": "FINISHED"}.get(status, status),
            "actualOrderId": row["orderId"] if Decimal(row["cumExecQty"]) > 0 else None,
            "orderType": order_type,
            "triggerPrice": row["triggerPrice"], "quantity": row["qty"],
            "closePosition": row["qty"] == "0" and row["reduceOnly"] and row["closeOnTrigger"], "createTime": int(row["createdTime"]),
        }
    return result


def normalize_trade(row: dict) -> dict:
    return {
        "symbol": row["symbol"], "id": row["tradeId"], "orderId": row["orderId"], "side": row["side"].upper(),
        "price": row["tradePrice"], "qty": row["qty"], "realizedPnl": row["cashFlow"],
        "commission": row["fee"], "commissionAsset": row["currency"], "time": int(row["transactionTime"]),
    }


class BybitDemoClient:
    exchange = "bybit"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.secret = api_secret.encode()
        self.http = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_SECONDS)
        self.time_offset = 0

    async def __aenter__(self) -> "BybitDemoClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict:
        # 只重发明确未受理的时间戳错误；超时的写请求由订单流水查询确认。
        for attempt in range(2):
            values = {k: v for k, v in (params or {}).items() if v is not None}
            payload = urlencode(values) if method == "GET" else json.dumps(values, separators=(",", ":"))
            timestamp = str(int(time.time() * 1000) + self.time_offset)
            signature = hmac.new(self.secret, f"{timestamp}{self.api_key}{RECV_WINDOW_MS}{payload}".encode(), hashlib.sha256).hexdigest()
            headers = {"X-BAPI-API-KEY": self.api_key, "X-BAPI-SIGN": signature,
                       "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS)}
            try:
                if method == "GET":
                    response = await self.http.get(f"{path}?{payload}" if payload else path, headers=headers)
                else:
                    response = await self.http.request(method, path, content=payload, headers={**headers, "Content-Type": "application/json"})
            except httpx.TransportError as e:
                raise ExchangeError(None, None, "无法连接 Bybit 模拟盘，请查询订单状态确认执行结果") from e
            try:
                return response_result(response)
            except ExchangeError as e:
                if e.code != 10002 or attempt:
                    message = e.message.replace(self.api_key, "[已隐藏]").replace(self.secret.decode(), "[已隐藏]")
                    raise ExchangeError(e.status, e.code, message) from None
                started = int(time.time() * 1000)
                server = await public_get("/v5/market/time")
                self.time_offset = int(server["timeNano"]) // 1_000_000 - (started + int(time.time() * 1000)) // 2
        raise AssertionError("unreachable")

    async def pages(self, path: str, params: dict) -> list[dict]:
        rows, cursor = [], None
        while True:
            result = await self.request("GET", path, {**params, "cursor": cursor, "limit": 50})
            rows.extend(result["list"])
            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                return rows
            if next_cursor == cursor:
                raise ExchangeError(None, None, "Bybit 分页游标没有推进，无法确认完整结果")
            cursor = next_cursor

    async def account(self) -> dict:
        result = await self.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
        coins = result["list"][0]["coin"]
        coin = next((c for c in coins if c["coin"] == "USDT"), None)
        if coin is None:
            raise ExchangeError(None, None, "Bybit 模拟盘没有 USDT 余额，请先在模拟账户领取资金")
        # 统一账户的总额以 USD 计价且逐仓时为空；策略只能使用 USDT 自身的余额和保证金。
        available = (Decimal(coin["walletBalance"]) - Decimal(coin["totalPositionIM"] or "0")
                     - Decimal(coin["totalOrderIM"] or "0") - Decimal(coin["locked"] or "0") - Decimal(coin["bonus"] or "0"))
        return {"totalWalletBalance": coin["walletBalance"], "totalMarginBalance": coin["equity"],
                "availableBalance": format(available, "f"), "totalUnrealizedProfit": coin["unrealisedPnl"]}

    async def raw_positions(self, symbol: str | None = None) -> list[dict]:
        return await self.pages("/v5/position/list", {"category": "linear", "settleCoin": "USDT", "symbol": symbol})

    async def positions(self) -> list[dict]:
        info = await self.request("GET", "/v5/account/info")
        isolated = info["marginMode"] == "ISOLATED_MARGIN"
        rows = await self.raw_positions()
        if any(p["positionIdx"] != 0 and Decimal(p["size"]) != 0 for p in rows):
            raise ExchangeError(None, None, "Bybit 有双向持仓，请先在模拟盘平仓并切换为单向持仓")
        return [{
            "symbol": p["symbol"], "positionAmt": ("-" if p["side"] == "Sell" else "") + p["size"],
            "entryPrice": p["avgPrice"] or "0", "breakEvenPrice": p["breakEvenPrice"] or p["avgPrice"] or "0",
            "markPrice": p["markPrice"], "unRealizedProfit": p["unrealisedPnl"], "liquidationPrice": p["liqPrice"] or "0",
            "notional": p["positionValue"], "isolatedMargin": p["positionIM"] if isolated else "0",
            "initialMargin": p["positionIM"],
        } for p in rows if Decimal(p["size"]) != 0]

    async def position_mode(self) -> bool:
        return any(p["positionIdx"] != 0 for p in await self.raw_positions())

    async def symbol_config(self, symbol: str) -> dict:
        info = await self.request("GET", "/v5/account/info")
        rows = await self.raw_positions(symbol)
        return {"leverage": int(Decimal(rows[0]["leverage"] or "0")) if rows else 0,
                "marginType": "ISOLATED" if info["marginMode"] == "ISOLATED_MARGIN" else "CROSSED"}

    async def ensure_setup(self, symbol: str) -> None:
        rows = await self.raw_positions(symbol)
        if any(p["positionIdx"] != 0 for p in rows):
            await self.request("POST", "/v5/position/switch-mode", {"category": "linear", "symbol": symbol, "mode": 0})
        config = await self.symbol_config(symbol)
        if config["marginType"] != "ISOLATED":
            await self.request("POST", "/v5/account/set-margin-mode", {"setMarginMode": "ISOLATED_MARGIN"})
        if config["leverage"] != 3:
            try:
                await self.request("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": symbol,
                                                                         "buyLeverage": "3", "sellLeverage": "3"})
            except ExchangeError as e:
                if e.code != 110043:
                    raise

    async def open_orders(self, symbol: str, is_algo: bool = False) -> list[dict]:
        rows = await self.pages("/v5/order/realtime", {"category": "linear", "symbol": symbol, "openOnly": 0})
        return [normalize_order(o, is_algo) for o in rows if bool(o.get("triggerPrice") and Decimal(o["triggerPrice"])) == is_algo]

    async def order(self, symbol: str, client_id: str, is_algo: bool = False) -> dict:
        params = {"category": "linear", "symbol": symbol, **({"orderId": client_id} if not OWN_CLIENT_ID.fullmatch(client_id) else {"orderLinkId": client_id})}
        rows = (await self.request("GET", "/v5/order/realtime", params))["list"]
        if not rows:
            rows = (await self.request("GET", "/v5/order/history", params))["list"]
        if not rows:
            raise ExchangeError(200, 110001, "Bybit 查无此单")
        return normalize_order(rows[0], is_algo)

    async def actual_order(self, symbol: str, order_id: str) -> dict:
        return await self.order(symbol, order_id)

    async def place_order(self, symbol: str, client_id: str, spec: OrderSpec, reduce_only: bool) -> dict:
        params = {"category": "linear", "symbol": symbol, "side": spec.side.title(),
                  "orderType": "Limit" if spec.type == "LIMIT" else "Market", "qty": format(spec.quantity, "f"),
                  "orderLinkId": client_id, "positionIdx": 0, "reduceOnly": reduce_only}
        if spec.type == "LIMIT":
            params |= {"price": format(spec.price, "f"), "timeInForce": "GTC"}
        if spec.type == "STOP_MARKET":
            params |= {"triggerPrice": format(spec.trigger_price, "f"), "triggerDirection": 1 if spec.side == "BUY" else 2,
                       "triggerBy": "MarkPrice", "closeOnTrigger": True}
        result = await self.request("POST", "/v5/order/create", params)
        # ACK 仅表示受理；保持 submitting，下一轮查询实际状态后才允许依赖止损的入场。
        return {"algoId": result["orderId"], "orderId": result["orderId"], "status": "SUBMITTING"}

    async def cancel_order(self, symbol: str, client_id: str, is_algo: bool = False) -> None:
        params = {"category": "linear", "symbol": symbol,
                  **({"orderId": client_id} if not OWN_CLIENT_ID.fullmatch(client_id) else {"orderLinkId": client_id})}
        try:
            await self.request("POST", "/v5/order/cancel", params)
        except ExchangeError as e:
            if e.code != 110001:
                raise

    async def trades(self, symbol: str | None, since_ms: int) -> list[dict]:
        # 资金流水提供逐笔已实现盈亏；execution/list 没有该字段，不能伪造为 0。
        now_ms = int(time.time() * 1000)
        rows = []
        while since_ms <= now_ms:
            end = min(now_ms, since_ms + 7 * 86400_000 - 1)
            page = await self.pages("/v5/account/transaction-log", {
                "accountType": "UNIFIED", "category": "linear", "currency": "USDT", "type": "TRADE",
                "startTime": since_ms, "endTime": end,
            })
            rows.extend(normalize_trade(t) for t in page if t["symbol"].endswith("USDT") and (symbol is None or t["symbol"] == symbol))
            since_ms = end + 1
        return sorted(rows, key=lambda t: (t["time"], t["id"]))


_instruments: tuple[float, dict[str, dict]] | None = None


async def instruments() -> dict[str, dict]:
    global _instruments
    if _instruments is None or time.monotonic() - _instruments[0] > EXCHANGE_INFO_SECONDS:
        parsed, cursor = {}, None
        while True:
            result = await public_get("/v5/market/instruments-info", {"category": "linear", "limit": 1000, **({"cursor": cursor} if cursor else {})})
            parsed.update({s["symbol"]: s for s in result["list"] if s["contractType"] == "LinearPerpetual" and s["settleCoin"] == "USDT" and s["status"] == "Trading"})
            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                break
            if next_cursor == cursor:
                raise ExchangeError(None, None, "Bybit 交易规则分页游标没有推进")
            cursor = next_cursor
        _instruments = time.monotonic(), parsed
    return _instruments[1]


async def symbol_rules(symbol: str) -> SymbolRules:
    info = (await instruments()).get(symbol)
    if info is None:
        raise ExchangeError(None, None, f"Bybit 模拟盘没有交易对 {symbol}")
    return SymbolRules(tick_size=step_decimal(info["priceFilter"]["tickSize"]),
                       step_size=step_decimal(info["lotSizeFilter"]["qtyStep"]),
                       min_notional=Decimal(info["lotSizeFilter"]["minNotionalValue"]))
