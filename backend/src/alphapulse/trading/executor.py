"""执行器：按「应有状态」对账。唯一会下单、撤单、推进计划状态的代码。

每轮对交易列表里的每个币：
1. 同步订单流水与交易所（成交、撤销、触发）。
2. 采纳分析器提交的限价计划（有持仓时作废）。
3. 推进计划状态，执行程序规则：限价单到期、时间止损、复核条件、无计划持仓。
4. 算出应有订单与现有订单对比：先挂止损，再撤多余，最后补缺（撤了旧入场单的这一轮不下新入场单）。
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.ai.tools import LOCAL_TZ
from alphapulse.core.crypto import decrypt
from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.market.binance import BinanceClient
from alphapulse.models import TradingAccount, TradingAnalysis, TradingEvent, TradingOrder, TradingSymbol
from alphapulse.trading.client import OWN_CLIENT_ID, DemoClient, ExchangeError, mark_price, symbol_rules
from alphapulse.trading.fills import sync_fills
from alphapulse.trading.plan import (
    MAX_HOLD_HOURS, TAKE_PROFIT_STEPS, OrderSpec, SymbolRules, condition_met, desired_orders, diff_orders, round_to_step,
)
from alphapulse.trading.schedule import request_analysis

logger = logging.getLogger("alphapulse.trading.executor")

EXECUTOR_SECONDS = 2
# 成交记录只用于历史仓位，不需要跟对账一样频繁
FILLS_SYNC_SECONDS = 60
ACTIVE_ORDER_STATUSES = ("submitting", "new", "partially_filled")
LIVE_PLAN_STATUSES = ("pending", "open", "closing")
# 刚下的单可能短时间查不到（C1 实测约 0.25s）
SUBMIT_GRACE_SECONDS = 10
# 止损挂单连续失败（可重试错误）达到次数后撤单平仓
STOP_FAILURE_LIMIT = 3
# 同一错误的时间线事件间隔，避免刷屏
ERROR_EVENT_SECONDS = 600
ORDER_STATUSES = {"NEW": "new", "PARTIALLY_FILLED": "partially_filled", "FILLED": "filled", "CANCELED": "canceled",
                  "EXPIRED": "expired", "EXPIRED_IN_MATCH": "expired", "REJECTED": "rejected"}
PURPOSE_LABELS = {"entry": "入场", "tp": "止盈", "stop": "止损", "close": "市价平仓"}
CLOSE_REASON_LABELS = {"stop": "止损", "take_profit": "止盈", "time": "到达持仓时限", "exit": "模型决定结束",
                       "external": "外部平仓（如在网页手动平仓）", "stop_failed": "止损挂单失败", "close": "市价平仓",
                       "manual_close": "一键平仓", "manual_cancel": "清除挂单"}
# 页面上的「平仓」「清除挂单」：API 写入请求，执行器下一轮取走执行，避免和对账同时改计划
MANUAL_ACTION_KEY = f"{KEY_PREFIX}trader:manual:{{}}"
MANUAL_ACTIONS = {"close": "一键平仓", "cancel": "清除挂单"}
CANCEL_REASON_LABELS = {"expired": "限价单到期未成交", "stopped": "已停止交易", "entry_failed": "入场单被拒"}


@dataclass
class Position:
    side: str  # long / short
    qty: Decimal
    entry: Decimal
    break_even: Decimal
    mark: Decimal


def fmt(value: Decimal) -> str:
    return format(value, "f")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Executor:
    def __init__(self) -> None:
        self.client: DemoClient | None = None
        self.key_ciphertext: bytes | None = None
        self.market = BinanceClient()
        # 已设置为逐仓 3 倍的币；单向持仓是账户级，只设一次
        self.configured: set[str] = set()
        self.one_way_checked = False
        self.stop_failures: dict[str, int] = {}
        self.error_events: dict[str, float] = {}
        # (symbol, 周期) -> (下一根收盘时间 ms, 最近收盘价)
        self.kline_cache: dict[tuple[str, str], tuple[int, Decimal]] = {}
        # 已提示过的无计划持仓：symbol -> 持仓均价，同一笔持仓只提示一次
        self.unmanaged_noticed: dict[str, Decimal] = {}
        self.fills_due = 0.0

    # ---------- 基础 ----------

    async def ensure_client(self, account: TradingAccount | None) -> DemoClient | None:
        ciphertext = account.api_key_encrypted if account else None
        if ciphertext != self.key_ciphertext:
            if self.client is not None:
                await self.client.http.aclose()
            self.client = None
            self.configured.clear()
            self.one_way_checked = False
            self.key_ciphertext = ciphertext
            if account is not None and ciphertext is not None:
                self.client = DemoClient(decrypt(ciphertext), decrypt(account.api_secret_encrypted))
        return self.client

    async def report(self, session: AsyncSession, symbol: str | None, message: str, level: str = "error", analysis_id: int | None = None) -> None:
        """同一条错误十分钟内只记一次"""
        key = f"{symbol}:{message}"
        if time.monotonic() - self.error_events.get(key, -ERROR_EVENT_SECONDS) < ERROR_EVENT_SECONDS:
            return
        self.error_events[key] = time.monotonic()
        logger.warning("%s %s", symbol, message)
        session.add(TradingEvent(symbol=symbol, analysis_id=analysis_id, kind="executor_error", level=level, message=message))

    async def last_close(self, symbol: str, interval: str) -> Decimal:
        """实盘该周期最近一根已收盘 K 线的收盘价；到下一根收盘前用缓存"""
        now_ms = int(time.time() * 1000)
        cached = self.kline_cache.get((symbol, interval))
        if cached and now_ms < cached[0]:
            return cached[1]
        rows = await self.market.get("um", "klines", {"symbol": symbol, "interval": interval, "limit": 2})
        closed = rows[-1] if rows[-1][6] < now_ms else rows[-2]
        # 下一根收盘时间 = 当前未收盘 K 线的 close_time
        next_close = rows[-1][6] + 1 if rows[-1][6] >= now_ms else now_ms
        self.kline_cache[(symbol, interval)] = (next_close, Decimal(closed[4]))
        return Decimal(closed[4])

    async def condition_met(self, symbol: str, condition: dict, mark: Decimal) -> tuple[bool, Decimal]:
        price = mark if condition["timeframe"] == "mark" else await self.last_close(symbol, condition["timeframe"])
        return condition_met(condition["direction"], Decimal(condition["price"]), price), price

    async def ensure_symbol_setup(self, client: DemoClient, symbol: str) -> None:
        """单向持仓、逐仓、3 倍杠杆。先查再改：有挂单时即使不需要修改，写操作也可能被拒（-4067）"""
        if not self.one_way_checked:
            if (await client.request("GET", "/fapi/v1/positionSide/dual"))["dualSidePosition"]:
                await client.request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": False})
            self.one_way_checked = True
        if symbol in self.configured:
            return
        config = (await client.request("GET", "/fapi/v1/symbolConfig", {"symbol": symbol}))[0]
        if config["marginType"] != "ISOLATED":
            await client.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        if config["leverage"] != 3:
            await client.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": 3})
        self.configured.add(symbol)

    # ---------- 订单 ----------

    async def place(self, session: AsyncSession, client: DemoClient, symbol: str, plan_id: int | None, purpose: str, spec: OrderSpec) -> ExchangeError | None:
        """先写流水并提交，再用 ap{id} 下单。结果不明（网络、5xx）时保持 submitting，下一轮按 id 查询，绝不换 id 重发"""
        row = TradingOrder(
            analysis_id=plan_id, symbol=symbol, purpose=purpose, leg=spec.leg, is_algo=spec.type == "STOP_MARKET",
            side=spec.side, type=spec.type, price=spec.price, trigger_price=spec.trigger_price, quantity=spec.quantity,
            reduce_only=purpose != "entry", status="submitting",
        )
        session.add(row)
        await session.commit()
        client_id = f"ap{row.id}"
        try:
            if row.is_algo:
                result = await client.request("POST", "/fapi/v1/algoOrder", {
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": spec.side, "type": "STOP_MARKET",
                    "triggerPrice": fmt(spec.trigger_price), "quantity": fmt(spec.quantity), "reduceOnly": True,
                    "workingType": "MARK_PRICE", "clientAlgoId": client_id,
                })
                row.exchange_id, row.status = str(result["algoId"]), "new"
            else:
                params = {"symbol": symbol, "side": spec.side, "type": spec.type, "quantity": fmt(spec.quantity),
                          "reduceOnly": row.reduce_only or None, "newClientOrderId": client_id}
                if spec.type == "LIMIT":
                    params |= {"price": fmt(spec.price), "timeInForce": "GTC"}
                result = await client.request("POST", "/fapi/v1/order", params)
                row.exchange_id = str(result["orderId"])
                row.status = ORDER_STATUSES.get(result["status"], result["status"].lower())
        except ExchangeError as e:
            row.error = str(e)
            if not (e.retryable or e.status is None):
                row.status = "rejected"
            await session.commit()
            return e
        await session.commit()
        return None

    async def cancel(self, client: DemoClient, row: TradingOrder) -> None:
        await self.cancel_client_id(client, row.symbol, f"ap{row.id}", row.is_algo)

    async def cancel_client_id(self, client: DemoClient, symbol: str, client_id: str, is_algo: bool) -> None:
        """撤单后不直接改状态：可能恰好已成交，交给下一轮同步确认"""
        try:
            if is_algo:
                await client.request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})
            else:
                await client.request("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        except ExchangeError as e:
            # -2011 / -2013：单已不在，等同步
            if e.code not in (-2011, -2013):
                raise

    async def sync_order(self, session: AsyncSession, client: DemoClient, row: TradingOrder, open_orders: dict, open_algos: dict) -> None:
        client_id = f"ap{row.id}"
        before = row.filled_qty
        try:
            if row.is_algo:
                if client_id in open_algos:
                    row.exchange_id, row.status = str(open_algos[client_id]["algoId"]), "new"
                    return
                algo = await client.request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})
                if algo["algoStatus"] in ("NEW", "TRIGGERING"):
                    row.status = "new"
                    return
                row.exchange_id = str(algo["algoId"])
                if algo["algoStatus"] == "FINISHED" and algo.get("actualOrderId"):
                    actual = await client.request("GET", "/fapi/v1/order", {"symbol": row.symbol, "orderId": algo["actualOrderId"]})
                    row.filled_qty = Decimal(actual["executedQty"])
                    row.avg_price = Decimal(actual["avgPrice"]) or None
                    row.status = "filled" if actual["status"] == "FILLED" else ORDER_STATUSES.get(actual["status"], "finished")
                    if row.status == "new":
                        # 触发后的市价单还没成交完，下一轮再查
                        return
                else:
                    row.status = algo["algoStatus"].lower()
            else:
                order = open_orders.get(client_id) or await client.request("GET", "/fapi/v1/order", {"symbol": row.symbol, "origClientOrderId": client_id})
                row.exchange_id = str(order["orderId"])
                row.status = ORDER_STATUSES.get(order["status"], order["status"].lower())
                row.filled_qty = Decimal(order["executedQty"])
                row.avg_price = Decimal(order["avgPrice"]) or None
        except ExchangeError as e:
            if e.code != -2013:
                raise
            if row.status == "submitting" and utcnow() - row.created_at < timedelta(seconds=SUBMIT_GRACE_SECONDS):
                return
            row.status, row.error = "failed", "交易所查无此单"
            return
        if row.filled_qty > before:
            delta = row.filled_qty - before
            leg = f" {row.leg + 1} " if row.purpose == "tp" else ""
            session.add(TradingEvent(
                symbol=row.symbol, analysis_id=row.analysis_id, kind=f"{row.purpose}_filled",
                level="warn" if row.purpose == "stop" else "info",
                message=f"{PURPOSE_LABELS[row.purpose]}{leg}成交 {fmt(delta)} @ {row.avg_price}",
                data={"order_id": row.id, "quantity": fmt(delta)},
            ))

    # ---------- 计划 ----------

    async def adopt(self, session: AsyncSession, client: DemoClient, symbol: TradingSymbol, current: TradingAnalysis | None,
                    position: Position | None) -> TradingAnalysis | None:
        """采纳最新的提议；较早的提议作废。返回采纳后的当前计划"""
        proposals = (await session.scalars(
            select(TradingAnalysis).where(TradingAnalysis.symbol == symbol.symbol, TradingAnalysis.plan_status == "proposed")
            .order_by(TradingAnalysis.id.desc())
        )).all()
        if not proposals:
            return current
        proposal, older = proposals[0], proposals[1:]
        for p in older:
            p.plan_status = "stale"
        now = utcnow()
        if proposal.action == "exit":
            proposal.plan_status = "applied"
            if current is not None:
                current.plan_status, current.close_reason = "closing", "exit"
                session.add(TradingEvent(symbol=symbol.symbol, analysis_id=current.id, kind="plan_exit", level="warn",
                                         message=f"按分析 #{proposal.id} 结束计划：撤销入场单，有持仓则市价平仓"))
            elif position is not None:
                session.add(TradingEvent(symbol=symbol.symbol, analysis_id=proposal.id, kind="plan_exit", level="warn",
                                         message=f"按分析 #{proposal.id} 市价平掉无计划的持仓 {fmt(position.qty)}"))
                close_side = "SELL" if position.side == "long" else "BUY"
                await self.place(session, client, symbol.symbol, proposal.id, "close", OrderSpec("close", None, close_side, "MARKET", None, None, position.qty))
            return current
        # 分析期间可能已经成交：持仓由程序管理，不能被新计划替换（平仓后会重新分析）
        qty = position.qty if position else Decimal(0)
        if qty > 0 or (current is not None and current.plan_status != "pending"):
            proposal.plan_status = "stale"
            reason = f"已有持仓 {fmt(qty)}" if qty > 0 else "计划正在平仓"
            session.add(TradingEvent(symbol=symbol.symbol, analysis_id=proposal.id, kind="plan_stale", level="warn",
                                     message=f"计划未采纳：{reason}"))
            return current
        if current is not None:
            current.plan_status, current.close_reason, current.closed_at = "replaced", "replaced", now
        proposal.plan_status = "pending"
        proposal.valid_until = now + timedelta(hours=float(proposal.decision["valid_hours"]))
        entry = proposal.sizing["entry"]
        side = "做多" if proposal.sizing["direction"] == "long" else "做空"
        session.add(TradingEvent(
            symbol=symbol.symbol, analysis_id=proposal.id, kind="plan_adopted",
            message=f"采纳计划 #{proposal.id}" + (f"，替换 #{current.id}" if current else "")
            + f"：限价{side} {entry['price']} × {entry['quantity']}，止损 {entry['stop']}（{entry['stop_pct']}%），"
            f"有效至 {proposal.valid_until.astimezone(LOCAL_TZ).strftime('%H:%M')}",
        ))
        return proposal

    async def realized(self, client: DemoClient, symbol: str, since: datetime | None) -> dict | None:
        if since is None:
            return None
        trades = await client.request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": int(since.timestamp() * 1000) - 1000, "limit": 1000})
        pnl = sum((Decimal(t["realizedPnl"]) for t in trades), Decimal(0))
        fee = sum((Decimal(t["commission"]) for t in trades if t["commissionAsset"] == "USDT"), Decimal(0))
        return {"realized_pnl": fmt(pnl), "commission": fmt(fee), "net": fmt(pnl - fee)}

    async def finish(self, session: AsyncSession, client: DemoClient, plan: TradingAnalysis, rows: list[TradingOrder]) -> None:
        """持仓归零：计划结束，记录原因和盈亏，排队分析"""
        if plan.close_reason is None or plan.plan_status == "open":
            filled = sorted((r for r in rows if r.filled_qty > 0 and r.purpose in ("stop", "tp", "close")), key=lambda r: r.updated_at)
            last = filled[-1].purpose if filled else None
            plan.close_reason = {"stop": "stop", "tp": "take_profit", "close": plan.close_reason or "close"}.get(last, "external")
        plan.plan_status = "closed" if plan.first_fill_at else "canceled"
        plan.closed_at = utcnow()
        plan.result = await self.realized(client, plan.symbol, plan.first_fill_at)
        net = f"，净盈亏 {plan.result['net']} USDT（含手续费 {plan.result['commission']}）" if plan.result else ""
        label = CLOSE_REASON_LABELS.get(plan.close_reason, plan.close_reason)
        session.add(TradingEvent(symbol=plan.symbol, analysis_id=plan.id, kind="plan_closed", level="warn" if plan.close_reason in ("stop", "stop_failed", "external") else "info",
                                 message=f"计划 #{plan.id} 结束：{label}{net}", data=plan.result))
        await request_analysis(session, plan.symbol, f"平仓（{label}）")

    async def cancel_plan(self, session: AsyncSession, plan: TradingAnalysis, reason: str, detail: str, level: str = "info") -> None:
        """未入场的计划撤销，排队重新分析（停止交易时 request_analysis 不排队）"""
        plan.plan_status, plan.close_reason, plan.closed_at = "canceled", reason, utcnow()
        session.add(TradingEvent(symbol=plan.symbol, analysis_id=plan.id, kind="plan_canceled", level=level,
                                 message=f"计划 #{plan.id} 撤销：{detail}"))
        await request_analysis(session, plan.symbol, f"计划撤销（{CANCEL_REASON_LABELS[reason]}）")

    async def start_closing(self, session: AsyncSession, plan: TradingAnalysis, reason: str, detail: str = "") -> None:
        plan.plan_status, plan.close_reason = "closing", reason
        session.add(TradingEvent(symbol=plan.symbol, analysis_id=plan.id, kind="plan_closing", level="warn",
                                 message=f"{CLOSE_REASON_LABELS[reason]}{detail}：撤销入场单并市价平仓"))

    async def review_once(self, session: AsyncSession, plan_id: int, symbol: str, kind: str, index: int, message: str, reason: str) -> None:
        """失效复核、reevaluate 每个条件只触发一次：以时间线事件去重"""
        exists = await session.scalar(select(func.count()).where(
            TradingEvent.analysis_id == plan_id, TradingEvent.kind == kind, TradingEvent.data["index"].as_integer() == index,
        ))
        if exists:
            return
        session.add(TradingEvent(symbol=symbol, analysis_id=plan_id, kind=kind, level="warn", message=message, data={"index": index}))
        await request_analysis(session, symbol, reason)

    # ---------- 对账 ----------

    async def reconcile(self, session: AsyncSession, client: DemoClient, symbol: TradingSymbol, running: bool, position: Position | None,
                        manual_action: str | None) -> None:
        name = symbol.symbol
        rules = await symbol_rules(name)
        open_orders = {o["clientOrderId"]: o for o in await client.request("GET", "/fapi/v1/openOrders", {"symbol": name})}
        open_algos = {o["clientAlgoId"]: o for o in await client.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": name})}

        rows = list((await session.scalars(
            select(TradingOrder).where(TradingOrder.symbol == name, TradingOrder.status.in_(ACTIVE_ORDER_STATUSES))
        )).all())
        for row in rows:
            await self.sync_order(session, client, row, open_orders, open_algos)
        await session.commit()

        # 交易所上本程序的单，但流水里不是活动状态：孤儿单，撤掉（用户手动下的单不动）
        active_ids = {f"ap{r.id}" for r in rows if r.status in ACTIVE_ORDER_STATUSES}
        for client_id, order in [*open_orders.items(), *open_algos.items()]:
            if OWN_CLIENT_ID.match(client_id) and client_id not in active_ids:
                await self.cancel_client_id(client, name, client_id, client_id in open_algos)
                session.add(TradingEvent(symbol=name, kind="orphan_canceled", level="warn", message=f"撤销孤儿单 {client_id}（{order.get('type') or order.get('orderType')}）"))

        plan = await session.scalar(
            select(TradingAnalysis).where(TradingAnalysis.symbol == name, TradingAnalysis.plan_status.in_(LIVE_PLAN_STATUSES))
            .order_by(TradingAnalysis.id.desc()).limit(1)
        )
        plan = await self.adopt(session, client, symbol, plan, position)
        await session.commit()
        if manual_action is not None:
            await self.manual(session, client, name, manual_action, plan, position, open_orders, open_algos)
            await session.commit()

        # 不属于当前计划的活动订单（被替换、已结束的计划）：入场和止盈先撤；止损等新止损挂上后再撤，不留无保护窗口
        stale = [r for r in rows if r.status in ACTIVE_ORDER_STATUSES and r.purpose != "close" and (plan is None or r.analysis_id != plan.id)]
        for row in stale:
            if row.purpose != "stop":
                await self.cancel(client, row)
        stale_entry_canceled = any(r.purpose == "entry" for r in stale)

        if plan is None:
            for row in stale:
                if row.purpose == "stop":
                    await self.cancel(client, row)
            if position is None:
                self.unmanaged_noticed.pop(name, None)
            elif running:
                await self.unmanaged(session, client, name, position, open_algos)
            if running:
                await self.check_reevaluate(session, name, position.mark if position else None)
            await session.commit()
            return

        direction = plan.sizing["direction"]
        if position is not None and position.side != direction:
            await self.report(session, name, f"持仓方向（{position.side}）与计划 #{plan.id} 方向（{direction}）不一致，暂停该币的下单", analysis_id=plan.id)
            await session.commit()
            return
        qty = position.qty if position else Decimal(0)
        now = utcnow()
        plan_rows = list((await session.scalars(select(TradingOrder).where(TradingOrder.analysis_id == plan.id))).all())

        if plan.plan_status == "pending":
            entry_rows = [r for r in plan_rows if r.purpose == "entry"]
            rejected = next((r for r in entry_rows if r.status in ("rejected", "failed")), None)
            # 成交后持仓可能已被止损或手动平掉：照样转入持仓，下一轮按持仓归零结束
            if qty > 0 or any(r.filled_qty > 0 for r in entry_rows):
                plan.plan_status, plan.first_fill_at = "open", now
                plan.close_deadline = now + timedelta(hours=MAX_HOLD_HOURS)
                session.add(TradingEvent(symbol=name, analysis_id=plan.id, kind="plan_open",
                                         message=f"限价单成交，计划 #{plan.id} 进入持仓；最晚 {plan.close_deadline.astimezone(LOCAL_TZ).strftime('%m-%d %H:%M')} 平仓"))
            elif not running:
                await self.cancel_plan(session, plan, "stopped", "已停止交易，撤销未成交的限价单")
            elif now >= plan.valid_until:
                await self.cancel_plan(session, plan, "expired", "限价单到期未成交")
            elif rejected:
                # 被拒后不重挂，免得每轮重复下同一张会被拒的单
                await self.cancel_plan(session, plan, "entry_failed", f"限价单被拒：{rejected.error or rejected.status}", "error")
        elif plan.plan_status == "open":
            if qty == 0:
                await self.finish(session, client, plan, plan_rows)
            elif now >= plan.close_deadline:
                await self.start_closing(session, plan, "time")
        elif plan.plan_status == "closing" and qty == 0:
            await self.finish(session, client, plan, plan_rows)
        await session.commit()

        if plan.plan_status in LIVE_PLAN_STATUSES:
            await self.sync_orders(session, client, plan, plan_rows, position, running, rules, stale_entry_canceled)
        else:
            # 计划已结束：只减仓止损不会被交易所自动撤销，必须主动撤，以免作用到之后的新仓位
            for row in plan_rows:
                if row.status in ACTIVE_ORDER_STATUSES and row.purpose != "close":
                    await self.cancel(client, row)
        for row in stale:
            if row.purpose == "stop":
                await self.cancel(client, row)
        if running and plan.plan_status != "closing":
            await self.check_reevaluate(session, name, position.mark if position else None)
        await session.commit()

    async def manual(self, session: AsyncSession, client: DemoClient, symbol: str, action: str, plan: TradingAnalysis | None,
                     position: Position | None, open_orders: dict, open_algos: dict) -> None:
        """一键平仓、清除挂单：撤掉该币全部挂单（含止损止盈和手动挂的），有持仓就市价平仓——程序不留没有止损的仓位。
        本程序的单通过结束计划撤销，否则下一轮对账会按计划补挂回来。撤销计划不像到期那样排队重新分析，免得马上又挂单"""
        reason = f"manual_{action}"
        label = MANUAL_ACTIONS[action]
        for client_id in [*open_orders, *open_algos]:
            if not OWN_CLIENT_ID.match(client_id):
                await self.cancel_client_id(client, symbol, client_id, client_id in open_algos)
        detail = f"，市价平仓 {fmt(position.qty)}" if position else ""
        session.add(TradingEvent(symbol=symbol, analysis_id=plan.id if plan else None, kind=reason, level="warn",
                                 message=f"{label}：撤销全部挂单{detail}"))
        if plan is not None and plan.plan_status in ("pending", "open") and position is not None:
            # 平仓中的计划：撤入场和止盈、市价平仓，持仓归零后结束并撤掉止损
            plan.plan_status, plan.close_reason = "closing", reason
        elif plan is not None and plan.plan_status == "pending":
            plan.plan_status, plan.close_reason, plan.closed_at = "canceled", reason, utcnow()
        elif plan is None and position is not None:
            closing = await session.scalar(select(func.count()).where(
                TradingOrder.symbol == symbol, TradingOrder.purpose == "close", TradingOrder.status.in_(ACTIVE_ORDER_STATUSES),
            ))
            if not closing:
                close_side = "SELL" if position.side == "long" else "BUY"
                await self.place(session, client, symbol, None, "close", OrderSpec("close", None, close_side, "MARKET", None, None, position.qty))

    async def check_reevaluate(self, session: AsyncSession, symbol: str, mark: Decimal | None) -> None:
        """最近一次交易中分析给出的 reevaluate 条件：满足任一个就排队分析"""
        latest = await session.scalar(
            select(TradingAnalysis).where(TradingAnalysis.symbol == symbol, TradingAnalysis.status == "done", TradingAnalysis.dry_run.is_(False))
            .order_by(TradingAnalysis.id.desc()).limit(1)
        )
        if latest is None or not latest.decision["reevaluate"]:
            return
        if mark is None and any(c["timeframe"] == "mark" for c in latest.decision["reevaluate"]):
            mark = await mark_price(symbol)
        for i, condition in enumerate(latest.decision["reevaluate"]):
            met, price = await self.condition_met(symbol, condition, mark)
            if met:
                text = f"{condition['timeframe']} {'高于' if condition['direction'] == 'above' else '低于'} {condition['price']}（现 {fmt(price)}）"
                await self.review_once(session, latest.id, symbol, "reevaluate_triggered", i, f"复核条件满足：{text}", f"复核条件满足：{text}")

    async def unmanaged(self, session: AsyncSession, client: DemoClient, symbol: str, position: Position, open_algos: dict) -> None:
        """有持仓但没有计划：有止损就只提示，没有止损就市价平仓"""
        close_side = "SELL" if position.side == "long" else "BUY"
        has_stop = any(o["side"] == close_side and o["orderType"].startswith("STOP") for o in open_algos.values())
        if has_stop:
            if self.unmanaged_noticed.get(symbol) != position.entry:
                self.unmanaged_noticed[symbol] = position.entry
                session.add(TradingEvent(symbol=symbol, kind="unmanaged_position", level="warn",
                                         message=f"{symbol} 有持仓 {fmt(position.qty)} 和止损但没有计划：程序不管理，由止损保护"))
            return
        closing = await session.scalar(select(func.count()).where(
            TradingOrder.symbol == symbol, TradingOrder.purpose == "close", TradingOrder.status.in_(ACTIVE_ORDER_STATUSES),
        ))
        if closing:
            return
        session.add(TradingEvent(symbol=symbol, kind="unmanaged_closed", level="error",
                                 message=f"{symbol} 有持仓 {fmt(position.qty)} 但没有计划也没有止损，市价平仓"))
        await self.place(session, client, symbol, None, "close", OrderSpec("close", None, close_side, "MARKET", None, None, position.qty))

    async def sync_orders(self, session: AsyncSession, client: DemoClient, plan: TradingAnalysis, plan_rows: list[TradingOrder],
                          position: Position | None, running: bool, rules: SymbolRules, block_entries: bool) -> None:
        sizing = plan.sizing
        qty = position.qty if position else Decimal(0)
        long = sizing["direction"] == "long"
        active = [r for r in plan_rows if r.status in ACTIVE_ORDER_STATUSES and r.purpose in ("entry", "stop", "tp")]
        if plan.plan_status == "closing":
            # 平仓中：撤入场和止盈，保留已有止损直到市价平仓成交（止损可能正因越过现价挂不上，不再补挂）
            for row in active:
                if row.purpose != "stop":
                    await self.cancel(client, row)
            if qty > 0 and not any(r.purpose == "close" and r.status in ACTIVE_ORDER_STATUSES for r in plan_rows):
                close_side = "SELL" if long else "BUY"
                await self.place(session, client, plan.symbol, plan.id, "close", OrderSpec("close", None, close_side, "MARKET", None, None, qty))
            return

        entry_rows = [r for r in plan_rows if r.purpose == "entry" and r.filled_qty > 0]
        filled = sum((r.filled_qty for r in entry_rows), Decimal(0))
        # 限价单撤了重挂时会有多行成交，按成交量加权
        average = sum((r.filled_qty * r.avg_price for r in entry_rows), Decimal(0)) / filled if filled else None
        tp_filled = [sum((r.filled_qty for r in plan_rows if r.purpose == "tp" and r.leg == j), Decimal(0))
                     for j in range(len(TAKE_PROFIT_STEPS[sizing["direction"]]))]
        stop = Decimal(sizing["entry"]["stop"])
        if tp_filled[0] > 0 and position is not None:
            # 止盈 1 后止损移到保本价：做多向上取整、做空向下取整，保证不亏；只收紧不放宽
            break_even = round_to_step(position.break_even, rules.tick_size, ROUND_CEILING if long else ROUND_FLOOR)
            stop = max(stop, break_even) if long else min(stop, break_even)

        # 止盈 1 成交后不再加仓
        entries_open = running and utcnow() < plan.valid_until and tp_filled[0] == 0
        desired = desired_orders(sizing, entries_open=entries_open, entry_filled=filled, entry_average=average,
                                 tp_filled=tp_filled, position_qty=qty, stop_price=stop, rules=rules)
        actual = [(r, OrderSpec(r.purpose, r.leg, r.side, r.type, r.price, r.trigger_price, r.quantity - r.filled_qty)) for r in active]
        to_cancel, to_place = diff_orders(desired, actual)

        entries = [d for d in to_place if d.purpose == "entry"]
        if entries:
            # 保证金模式和杠杆在有挂单时不能改，必须在挂任何单之前设好
            await self.ensure_symbol_setup(client, plan.symbol)
        # 1. 止损先挂新的再撤旧的（只减仓止损可以并存），任何时候都不留无保护窗口
        for spec in [d for d in to_place if d.purpose == "stop"]:
            error = await self.place(session, client, plan.symbol, plan.id, "stop", spec)
            if error is not None:
                await self.stop_failed(session, plan, error)
                return
        self.stop_failures.pop(plan.symbol, None)
        # 2. 撤多余的单
        canceled_entry = False
        for row in to_cancel:
            await self.cancel(client, row)
            canceled_entry = canceled_entry or row.purpose == "entry"
        # 3. 补止盈；入场单只在本轮没有撤旧入场单、且止损在场时下
        has_stop = any(r.purpose == "stop" for r in active if r not in to_cancel) or any(d.purpose == "stop" for d in to_place)
        for spec in [d for d in to_place if d.purpose == "tp"]:
            await self.place(session, client, plan.symbol, plan.id, "tp", spec)
        if entries and has_stop and not canceled_entry and not block_entries:
            for spec in entries:
                await self.place(session, client, plan.symbol, plan.id, "entry", spec)

    async def stop_failed(self, session: AsyncSession, plan: TradingAnalysis, error: ExchangeError) -> None:
        """会立即触发（-2021）或不可重试的错误：撤单平仓；可重试的错误连续失败多次后同样处理"""
        if error.retryable or error.status is None:
            count = self.stop_failures[plan.symbol] = self.stop_failures.get(plan.symbol, 0) + 1
            if count < STOP_FAILURE_LIMIT:
                await self.report(session, plan.symbol, f"挂止损失败（第 {count} 次），下一轮重试：{error}", "warn", plan.id)
                return
        self.stop_failures.pop(plan.symbol, None)
        reason = "止损价已越过现价" if error.code == -2021 else f"挂止损失败：{error}"
        session.add(TradingEvent(symbol=plan.symbol, analysis_id=plan.id, kind="stop_failed", level="error", message=reason))
        await self.start_closing(session, plan, "stop_failed")

    # ---------- 主循环 ----------

    async def tick(self) -> None:
        async with SessionLocal() as session:
            account = await session.get(TradingAccount, 1)
            symbols = list((await session.scalars(select(TradingSymbol))).all())
            client = await self.ensure_client(account)
            if client is None:
                return
            positions = {}
            for p in await client.request("GET", "/fapi/v3/positionRisk"):
                amount = Decimal(p["positionAmt"])
                if amount != 0:
                    positions[p["symbol"]] = Position(
                        side="long" if amount > 0 else "short", qty=abs(amount), entry=Decimal(p["entryPrice"]),
                        break_even=Decimal(p["breakEvenPrice"]), mark=Decimal(p["markPrice"]),
                    )
            # 交易列表为空时也要同步：手动成交同样计入历史仓位。
            # 用独立会话：失败回滚会让会话里的对象全部过期，不能影响下面的对账
            if time.monotonic() >= self.fills_due:
                self.fills_due = time.monotonic() + FILLS_SYNC_SECONDS
                try:
                    async with SessionLocal() as fills_session:
                        fetched = await sync_fills(fills_session, client, {s.symbol for s in symbols} | set(positions))
                    if fetched:
                        logger.info("同步成交记录 %d 条", fetched)
                except Exception:
                    logger.warning("同步成交记录失败，%ds 后重试", FILLS_SYNC_SECONDS, exc_info=True)
            for symbol in symbols:
                try:
                    manual_action = await redis.getdel(MANUAL_ACTION_KEY.format(symbol.symbol))
                    await self.reconcile(session, client, symbol, symbol.running, positions.get(symbol.symbol), manual_action)
                except ExchangeError as e:
                    await session.rollback()
                    await self.report(session, symbol.symbol, f"对账失败：{e}")
                    await session.commit()
                except Exception as e:
                    logger.exception("%s 对账出错", symbol.symbol)
                    await session.rollback()
                    await self.report(session, symbol.symbol, f"对账出错：{e!r}")
                    await session.commit()


async def executor_loop() -> None:
    executor = Executor()
    while True:
        try:
            await executor.tick()
        except ExchangeError as e:
            logger.warning("执行器同步失败：%s", e)
        except Exception:
            logger.exception("执行器出错，稍后重试")
        await asyncio.sleep(EXECUTOR_SECONDS)
