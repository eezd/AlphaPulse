"""币安公开行情接口（无需 API Key）"""

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("alphapulse.market.binance")

REST = {
    "spot": {"base": "https://api.binance.com", "klines": "/api/v3/klines", "exchange_info": "/api/v3/exchangeInfo",
             "ticker": "/api/v3/ticker/24hr", "weight_limit": 6000, "kline_limit": 1000},
    "um": {"base": "https://fapi.binance.com", "klines": "/fapi/v1/klines", "exchange_info": "/fapi/v1/exchangeInfo",
           "ticker": "/fapi/v1/ticker/24hr", "weight_limit": 2400, "kline_limit": 1500},
}
WS = {"spot": "wss://stream.binance.com:9443/stream", "um": "wss://fstream.binance.com/stream"}

# 权重用量超过上限的这个比例就等到下一分钟
WEIGHT_RATIO = 0.5
MAX_RETRIES = 3


def decimals(step: str) -> int:
    """"0.01000000" -> 2，"1.0" -> 0"""
    return len(step.rstrip("0").split(".")[1]) if "." in step.rstrip("0") else 0


class BinanceClient:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=20)

    async def get(self, market: str, path_key: str, params: dict[str, Any] | None = None) -> Any:
        conf = REST[market]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self.http.get(conf["base"] + conf[path_key], params=params)
            except httpx.TransportError as e:
                if attempt == MAX_RETRIES:
                    raise
                logger.warning("币安请求网络错误（第 %d 次）：%r", attempt, e)
                await asyncio.sleep(2 * attempt)
                continue
            if resp.status_code in (418, 429):
                # 418 表示 IP 已被临时封禁，必须按 Retry-After 等待
                wait = int(resp.headers.get("Retry-After", "60"))
                logger.warning("币安限流 %d，暂停 %ds", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                await asyncio.sleep(2 * attempt)
                continue
            resp.raise_for_status()
            used = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", "0"))
            if used > conf["weight_limit"] * WEIGHT_RATIO:
                wait = 61 - time.time() % 60
                logger.info("%s 权重已用 %d，等待 %.0fs 到下一分钟", market, used, wait)
                await asyncio.sleep(wait)
            return resp.json()
        raise RuntimeError(f"币安请求重试 {MAX_RETRIES} 次仍被限流：{path_key} {params}")

    async def klines(self, market: str, symbol: str, interval: str, start_time: int, end_time: int | None = None) -> list[list]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "startTime": start_time, "limit": REST[market]["kline_limit"]}
        if end_time is not None:
            params["endTime"] = end_time
        return await self.get(market, "klines", params)

    async def tradable_symbols(self, market: str) -> list[dict[str, Any]]:
        """可交易的交易对；合约只保留永续（含 TRADIFI_PERPETUAL 等美股永续）"""
        info = await self.get(market, "exchange_info")
        result = []
        for s in info["symbols"]:
            if s["status"] != "TRADING" or (market == "um" and not s["contractType"].endswith("PERPETUAL")):
                continue
            filters = {f["filterType"]: f for f in s["filters"]}
            result.append({
                "symbol": s["symbol"],
                "base": s["baseAsset"],
                "quote": s["quoteAsset"],
                "price_precision": decimals(filters["PRICE_FILTER"]["tickSize"]),
                "volume_precision": decimals(filters["LOT_SIZE"]["stepSize"]),
            })
        return result

    async def ticker(self, market: str, symbol: str) -> dict[str, Any]:
        return await self.get(market, "ticker", {"symbol": symbol})
