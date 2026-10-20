import asyncio
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest

from alphapulse.trading.bybit import BybitDemoClient, normalize_order, normalize_trade
from alphapulse.trading.client import ExchangeError
from alphapulse.trading.plan import OrderSpec


@pytest.mark.parametrize("method,params", [("GET", {"symbol": "BTCUSDT", "cursor": "a:b+="}), ("POST", {"reduceOnly": True, "qty": "0.001"})])
def test_signature_matches_exact_transmitted_payload(method, params):
    def handle(request):
        payload = request.url.query.decode() if method == "GET" else request.content.decode()
        headers = request.headers
        signed = headers["X-BAPI-TIMESTAMP"] + "test-key" + headers["X-BAPI-RECV-WINDOW"] + payload
        assert headers["X-BAPI-SIGN"] == hmac.new(b"test-secret", signed.encode(), hashlib.sha256).hexdigest()
        assert request.url.host == "api-demo.bybit.com"
        if method == "POST":
            assert json.loads(payload) == params
        return httpx.Response(200, json={"retCode": 0, "result": {"confirmed": True}})

    async def check():
        async with BybitDemoClient("test-key", "test-secret") as client:
            await client.http.aclose()
            client.http = httpx.AsyncClient(base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handle))
            assert await client.request(method, "/v5/test", params) == {"confirmed": True}
    asyncio.run(check())


def test_conditional_stop_ack_keeps_execution_unknown():
    def handle(request):
        params = json.loads(request.content)
        assert (params["orderType"], params["triggerDirection"], params["triggerBy"]) == ("Market", 2, "MarkPrice")
        assert params["reduceOnly"] and params["closeOnTrigger"]
        assert params["positionIdx"] == 0
        assert params["orderLinkId"] == "ap42"
        return httpx.Response(200, json={"retCode": 0, "result": {"orderId": "7b6a537e-6f74-43d9-b775-35d0521c20de"}})

    async def check():
        async with BybitDemoClient("test-key", "test-secret") as client:
            await client.http.aclose()
            client.http = httpx.AsyncClient(base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handle))
            result = await client.place_order("BTCUSDT", "ap42", OrderSpec("stop", None, "SELL", "STOP_MARKET", None, Decimal("70000"), Decimal("0.001")), True)
            assert result["status"] == "SUBMITTING"
    asyncio.run(check())


def test_filled_conditional_order_preserves_execution_and_uuid():
    order_id = "7b6a537e-6f74-43d9-b775-35d0521c20de"
    result = normalize_order({
        "symbol": "BTCUSDT", "orderId": order_id, "orderLinkId": "ap42", "side": "Sell", "orderType": "Market",
        "orderStatus": "Filled", "price": "", "qty": "0.001", "cumExecQty": "0.001", "avgPrice": "70000",
        "reduceOnly": True, "createdTime": "1792400000000", "triggerPrice": "70100",
        "stopOrderType": "Stop", "triggerDirection": 2, "closeOnTrigger": True,
    }, True)
    assert (result["status"], result["executedQty"], result["avgPrice"]) == ("FILLED", "0.001", "70000")
    assert result["actualOrderId"] == result["algoId"] == order_id


def test_trade_uses_realized_cashflow_without_subtracting_fee_twice():
    result = normalize_trade({"symbol": "BTCUSDT", "tradeId": "trade-uuid", "orderId": "order-uuid", "side": "Sell",
                              "tradePrice": "81000", "qty": "0.001", "cashFlow": "1", "fee": "0.04", "currency": "USDT",
                              "transactionTime": "1792400000000"})
    assert (result["realizedPnl"], result["commission"], result["id"]) == ("1", "0.04", "trade-uuid")


def test_timeout_response_is_unknown_and_not_resent():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"retCode": 10000, "retMsg": "Server Timeout", "result": {}})

    async def check():
        async with BybitDemoClient("test-key", "test-secret") as client:
            await client.http.aclose()
            client.http = httpx.AsyncClient(base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handle))
            with pytest.raises(ExchangeError) as error:
                await client.request("POST", "/v5/order/create", {"orderLinkId": "ap42"})
            assert error.value.retryable
            assert len(calls) == 1
    asyncio.run(check())


def test_binance_backend_timeout_is_not_definitive_rejection():
    assert ExchangeError(408, -1007, "Timeout").retryable
