import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alphapulse.trading.bybit import normalize_order
from alphapulse.trading.client import ExchangeError
from alphapulse.trading.plan import SymbolRules


class Session:
    def __init__(self, scalar=None, groups=()):
        self.current = scalar
        self.groups = iter(groups)
        self.added = []

    def add(self, row):
        if row.id is None:
            row.id = 100 + len(self.added)
        self.added.append(row)

    async def commit(self):
        for row in self.added:
            if hasattr(row, "filled_qty") and row.filled_qty is None:
                row.filled_qty = Decimal(0)

    async def scalar(self, query):
        return self.current

    async def scalars(self, query):
        class Rows(list):
            def all(self):
                return self
        return Rows(next(self.groups))


class Client:
    exchange = "bybit"

    def __init__(self):
        self.canceled = []
        self.created = []
        self.position_rows = []
        self.entry_status = "NEW"

    async def place_order(self, symbol, client_id, spec, reduce_only):
        self.created.append(spec)
        return {"orderId": client_id, "algoId": client_id, "status": "SUBMITTING"}

    async def cancel_order(self, symbol, client_id, is_algo):
        self.canceled.append(client_id)

    async def open_orders(self, symbol, is_algo=False):
        return []

    async def order(self, symbol, client_id, is_algo=False):
        return {"orderId": client_id, "status": self.entry_status,
                "executedQty": "1" if self.entry_status == "FILLED" else "0", "avgPrice": "100"}

    async def positions(self):
        return self.position_rows


def plan(trading):
    return trading.TradingAnalysis(id=1, symbol="BTCUSDT", plan_status="pending", exchange="bybit",
                                   sizing={"direction": "long", "entry": {"quantity": "1", "price": "100", "stop": "90"}},
                                   valid_until=datetime.now(UTC) + timedelta(hours=1))


def order(trading, purpose, **values):
    return trading.TradingOrder(id=2 if purpose == "entry" else 3, analysis_id=1, exchange="bybit", symbol="BTCUSDT",
                                purpose=purpose, side="BUY" if purpose == "entry" else "SELL", leg=0 if purpose == "entry" else None,
                                type="LIMIT" if purpose == "entry" else "STOP_MARKET", price=Decimal(100) if purpose == "entry" else None,
                                trigger_price=Decimal(80) if purpose == "stop" else None, quantity=Decimal(1), filled_qty=Decimal(0),
                                is_algo=purpose == "stop", status="new", **values)


RULES = SymbolRules(Decimal("0.1"), Decimal("0.01"), Decimal(5))


@pytest.mark.parametrize("has_setup", [False, True])
@pytest.mark.parametrize("status", ["open", "closed"])
def test_recent_trade_brief_preserves_acceptance_fills_without_setup(trading, has_setup, status):
    from alphapulse.trading.analyst import trade_brief

    filled_at = datetime(2026, 10, 20, 8, 58, tzinfo=UTC)
    sizing = {"direction": "long", "entry": {"price": "2380.78"}}
    decision = {"action": "plan", "reevaluate": [], "valid_hours": "0.1667"}
    if has_setup:
        setup = {"zone_low": "2375", "zone_high": "2385"}
        sizing["setup"] = setup
        decision.update(direction="long", setup={**setup, "entry_price": "2380.78", "basis": "支撑区"})
    row = trading.TradingAnalysis(
        id=239, decision=decision, sizing=sizing, plan_status=status,
        first_fill_at=filled_at, closed_at=filled_at + timedelta(seconds=24),
        close_reason="manual", result={"net": "-0.00336383"},
    )

    text = trade_brief(row)

    zone = "区间 2375–2385" if has_setup else "区间未记录"
    assert text.startswith(f"#239 做多，{zone}，挂单 2380.78，2026-10-20 16:58 成交")
    assert text.endswith("净 -0.00 USDT" if status == "closed" else "持仓中")


def test_stop_replacement_waits_for_exchange_confirmation(trading):
    async def check():
        engine, client, session = trading.Executor(), Client(), Session()
        try:
            p, old = plan(trading), order(trading, "stop")
            p.plan_status = "open"
            position = trading.Position("long", Decimal(1), Decimal(100), Decimal(101))
            assert not await engine.sync_orders(session, client, p, [old], position, False, RULES, False)
            assert client.canceled == []
            [replacement] = [r for r in session.added if isinstance(r, trading.TradingOrder)]
            assert replacement.status == "submitting"
            replacement.status = "new"
            assert await engine.sync_orders(session, client, p, [old, replacement], position, False, RULES, False)
            assert client.canceled == ["ap3"]
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("exchange,direction,average,stop,quantity,tp_qty,expected", [
    ("bybit", "long", "2394.5", "2390.3", "0.81", "0.48", "2396.30"),
    ("bybit", "short", "2403.1", "2410.68", "0.52", "0.41", "2401.29"),
    ("binance", "long", "2394.5", "2390.3", "0.81", "0.48", "2395.94"),
    ("binance", "short", "2403.1", "2410.68", "0.52", "0.41", "2401.65"),
])
def test_tp1_protects_remaining_cost_and_keeps_confirmed_tighter_stop(trading, exchange, direction, average, stop, quantity, tp_qty, expected):
    async def check():
        engine, client = trading.Executor(), Client()
        client.exchange = engine.exchange = exchange
        p, entry, old = plan(trading), order(trading, "entry"), order(trading, "stop")
        p.plan_status = "open"
        p.sizing = {"direction": direction, "entry": {"quantity": quantity, "price": average, "stop": stop}}
        entry.exchange_id, entry.filled_qty, entry.avg_price, entry.status = "entry-id", Decimal(quantity), Decimal(average), "filled"
        old.trigger_price, old.quantity = Decimal(stop), Decimal(quantity)
        old.side = "SELL" if direction == "long" else "BUY"
        tp = trading.TradingOrder(purpose="tp", leg=0, filled_qty=Decimal(tp_qty), status="filled")
        fill = SimpleNamespace(order_id="entry-id", qty=entry.filled_qty, commission_asset="USDT",
                               commission=entry.filled_qty * entry.avg_price * Decimal("0.0002"))
        session = Session(groups=[[fill], [fill], [fill]])
        position = trading.Position(direction, Decimal(quantity) - Decimal(tp_qty), Decimal(average), Decimal(average))
        rules = SymbolRules(Decimal("0.01"), Decimal("0.01"), Decimal(5))
        try:
            await engine.sync_orders(session, client, p, [entry, old, tp], position, True, rules, False)
            replacement = next(r for r in session.added if isinstance(r, trading.TradingOrder) and r.purpose == "stop")
            assert replacement.trigger_price == Decimal(expected)
            # Bybit ACK 尚未确认，原止损继续保护；币安条件单同步确认后即可撤旧单。
            assert client.canceled == ([] if exchange == "bybit" else ["ap3"])
            replacement.status = "new"
            client.canceled.clear()
            await engine.sync_orders(session, client, p, [entry, old, tp, replacement], position, True, rules, False)
            assert client.canceled == ["ap3"]
            # 已存在更紧的保护时不放宽，也不因余仓变少而用止盈利润抵扣成本。
            replacement.trigger_price += Decimal(1) if direction == "long" else Decimal(-1)
            client.created.clear()
            await engine.sync_orders(session, client, p, [entry, tp, replacement], position, True, rules, False)
            assert all(spec.purpose not in ("stop", "entry") for spec in client.created)
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


def test_missing_entry_fees_leave_existing_protection_untouched(trading, monkeypatch):
    async def check():
        engine, client = trading.Executor(), Client()
        p, entry, old = plan(trading), order(trading, "entry"), order(trading, "stop")
        entry.exchange_id, entry.filled_qty, entry.avg_price = "entry-id", Decimal(1), Decimal(100)
        entry.status = "filled"
        entry.created_at = datetime.now(UTC)
        tp = trading.TradingOrder(purpose="tp", leg=0, filled_qty=Decimal("0.6"), status="filled")
        monkeypatch.setattr(client, "trades", AsyncMock(return_value=[]), raising=False)
        monkeypatch.setattr(trading, "save_fills", AsyncMock())
        try:
            with pytest.raises(ExchangeError, match="手续费尚未完整同步"):
                await engine.sync_orders(Session(groups=[[], []]), client, p, [entry, old, tp],
                                         trading.Position("long", Decimal("0.4"), Decimal(100), Decimal(101)), True, RULES, False)
            assert client.created == client.canceled == []
            assert client.trades.await_count == 1
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
@pytest.mark.parametrize("fees_complete", [False, True])
def test_tp1_cancels_remaining_entry_before_fee_lookup(trading, monkeypatch, exchange, fees_complete):
    async def check():
        engine, client = trading.Executor(), Client()
        client.exchange = engine.exchange = exchange
        p, entry, old = plan(trading), order(trading, "entry"), order(trading, "stop")
        p.plan_status = "open"
        entry.exchange_id, entry.filled_qty, entry.avg_price = "entry-id", Decimal("0.5"), Decimal(100)
        entry.status, entry.created_at = "partially_filled", datetime.now(UTC)
        old.trigger_price = Decimal(90)
        tp = trading.TradingOrder(purpose="tp", leg=0, filled_qty=Decimal("0.3"), status="filled")
        fill = SimpleNamespace(order_id="entry-id", qty=entry.filled_qty, commission_asset="USDT", commission=Decimal("0.01"))
        session = Session(groups=[[fill]] if fees_complete else [[], []])
        position = trading.Position("long", Decimal("0.2"), Decimal(100), Decimal(101))

        async def trades(*args):
            # 补取仍缺手续费时，撤入场单也必须先完成。
            assert client.canceled == ["ap2"]
            return []

        monkeypatch.setattr(client, "trades", AsyncMock(side_effect=trades), raising=False)
        monkeypatch.setattr(trading, "save_fills", AsyncMock())
        try:
            if fees_complete:
                await engine.sync_orders(session, client, p, [entry, old, tp], position, True, RULES, False)
            else:
                with pytest.raises(ExchangeError, match="手续费尚未完整同步"):
                    await engine.sync_orders(session, client, p, [entry, old, tp], position, True, RULES, False)
                assert client.created == []
                assert client.canceled == ["ap2"]
            assert client.canceled.count("ap2") == 1
            assert all(spec.purpose != "entry" for spec in client.created)
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("asset", ["USDT", "BNB"])
def test_entry_fees_recover_complete_partial_fills_without_using_realized_profit(trading, monkeypatch, asset):
    async def check():
        engine, client = trading.Executor(), Client()
        entry = order(trading, "entry")
        entry.exchange_id, entry.filled_qty = "entry-id", Decimal(1)
        entry.created_at = datetime.now(UTC)
        fills = [SimpleNamespace(order_id="entry-id", qty=Decimal("0.4"), commission_asset=asset, commission=Decimal("0.008")),
                 SimpleNamespace(order_id="entry-id", qty=Decimal("0.6"), commission_asset=asset, commission=Decimal("0.033"))]
        session = Session(groups=[fills[:1], fills])
        monkeypatch.setattr(client, "trades", AsyncMock(return_value=[]), raising=False)
        monkeypatch.setattr(trading, "save_fills", AsyncMock())
        try:
            if asset == "USDT":
                assert await engine.entry_fee_per_unit(session, client, "BTCUSDT", [entry]) == Decimal("0.041")
            else:
                with pytest.raises(ExchangeError, match="手续费不是 USDT"):
                    await engine.entry_fee_per_unit(session, client, "BTCUSDT", [entry])
            assert client.trades.await_count == 1
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


def test_rejected_replacement_does_not_cancel_existing_stop(trading, monkeypatch):
    async def reject(*args):
        raise ExchangeError(400, 110092, "止损被拒")
    async def check():
        engine, client, session = trading.Executor(), Client(), Session()
        monkeypatch.setattr(client, "place_order", reject)
        p = plan(trading)
        try:
            protected = await engine.sync_orders(session, client, p, [order(trading, "stop")],
                                                  trading.Position("long", Decimal(1), Decimal(100), Decimal(101)), False, RULES, False)
            assert not protected and client.canceled == []
            assert p.plan_status == "closing" and p.close_reason == "stop_failed"
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


def test_stopped_pending_plan_keeps_late_fill_and_stop(trading, monkeypatch):
    async def rules(*args):
        return RULES
    monkeypatch.setattr(trading, "symbol_rules", rules)

    async def check():
        engine, client = trading.Executor(), Client()
        p, entry, stop = plan(trading), order(trading, "entry"), order(trading, "stop")
        stop.trigger_price = Decimal(90)
        symbol = SimpleNamespace(symbol="BTCUSDT")
        # 首轮撤单只收到 ACK，计划不能结束，也不能撤保护止损。
        session = Session(p, [[entry, stop], [], [entry, stop]])
        async def sync(session, client, row, *args):
            if row.purpose == "entry" and client.entry_status == "FILLED":
                row.status, row.filled_qty, row.avg_price = "filled", Decimal(1), Decimal(100)
        monkeypatch.setattr(engine, "sync_order", sync)
        try:
            await engine.reconcile(session, client, symbol, False, None)
            assert p.plan_status == "closing" and p.closed_at is None
            assert "ap2" in client.canceled and "ap3" not in client.canceled
            client.entry_status = "FILLED"
            client.position_rows = [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100", "breakEvenPrice": "100", "markPrice": "101"}]
            await engine.reconcile(Session(p, [[entry, stop], [], [entry, stop]]), client, symbol, False, None)
            assert p.plan_status == "open" and p.first_fill_at is not None
            assert all(spec.type != "MARKET" for spec in client.created)
            assert "ap3" not in client.canceled
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("stop_type,direction,expected", [("TakeProfit", 1, "TAKE_PROFIT_MARKET"), ("Stop", 1, "TAKE_PROFIT_MARKET"), ("Stop", 2, "STOP_MARKET")])
def test_bybit_conditional_profit_is_not_a_protective_stop(stop_type, direction, expected):
    result = normalize_order({"symbol": "BTCUSDT", "orderId": "external", "orderLinkId": "", "side": "Sell", "orderType": "Market",
                              "orderStatus": "Untriggered", "price": "", "qty": "1", "cumExecQty": "0", "avgPrice": "",
                              "reduceOnly": True, "createdTime": "1792400000000", "triggerPrice": "110", "stopOrderType": stop_type,
                              "triggerDirection": direction}, True)
    assert result["orderType"] == expected


@pytest.mark.parametrize("kind,quantity,reduce_only", [("TAKE_PROFIT_MARKET", "1", True), ("STOP_MARKET", "0.1", True), ("STOP_MARKET", "1", False)])
def test_unmanaged_requires_full_reduce_only_stop(trading, kind, quantity, reduce_only):
    async def check():
        engine, session, client = trading.Executor(), Session(0), Client()
        try:
            await engine.unmanaged(session, client, "BTCUSDT", trading.Position("long", Decimal(1), Decimal(100), Decimal(101)),
                                   {"external": {"side": "SELL", "orderType": kind, "quantity": quantity, "reduceOnly": reduce_only, "closePosition": False}})
            assert [spec.type for spec in client.created] == ["MARKET"]
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


def test_analysis_snapshot_rejects_recent_error_and_old_success(trading):
    from alphapulse.trading.snapshot import SnapshotError, require_fresh_snapshot
    snapshot = {"account": {"margin_balance": "100"}, "error": None, "synced_at": datetime.now(UTC).isoformat()}
    assert require_fresh_snapshot(snapshot) is snapshot
    for change in ({"error": "网络错误"}, {"synced_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}):
        with pytest.raises(SnapshotError):
            require_fresh_snapshot(snapshot | change)


@pytest.mark.parametrize("unavailable", ["missing", "error", "stale"])
@pytest.mark.parametrize("reasons", [["例行分析"], ["手动分析"]])
def test_analysis_request_waits_for_fresh_snapshot(trading, monkeypatch, unavailable, reasons):
    from alphapulse.trading import engine

    requested_at = datetime.now(UTC) - timedelta(seconds=1)
    target = trading.TradingSymbol(symbol="BTCUSDT", analysis_requested_at=requested_at, analysis_reasons=list(reasons))
    fresh = {"account": {"margin_balance": "100"}, "error": None, "synced_at": datetime.now(UTC).isoformat()}
    snapshot = {
        "missing": None,
        "error": fresh | {"error": "账户同步失败"},
        "stale": fresh | {"synced_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
    }[unavailable]

    class QueueSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalars(self, query):
            return SimpleNamespace(all=lambda: [target] if target.analysis_requested_at is not None else [])

        async def commit(self):
            pass

    async def read_snapshot():
        return snapshot

    monkeypatch.setattr(engine, "SessionLocal", QueueSession)
    monkeypatch.setattr(engine, "read_snapshot", read_snapshot)

    async def check():
        nonlocal snapshot
        assert await engine.claim_requests(1, set()) == []
        assert target.analysis_requested_at == requested_at
        assert target.analysis_reasons == reasons
        snapshot = fresh
        assert await engine.claim_requests(1, set()) == [("BTCUSDT", reasons)]
        assert target.analysis_requested_at is None and target.analysis_reasons == []
        assert await engine.claim_requests(1, set()) == []

    asyncio.run(check())


def test_manual_request_survives_failure_before_execution(trading, monkeypatch):
    from alphapulse.api import trading as api

    async def get_symbol(*args):
        return SimpleNamespace(symbol="BTCUSDT")

    async def fresh(*args):
        return {}

    async def exchange(*args):
        return "bybit"

    async def rules(*args):
        return RULES

    monkeypatch.setattr(api, "get_symbol", get_symbol)
    monkeypatch.setattr(api, "fresh_snapshot", fresh)
    monkeypatch.setattr(api, "active_exchange", exchange)
    monkeypatch.setattr(trading, "symbol_rules", rules)

    async def check():
        session = Session()
        await api.manual_action("BTCUSDT", "cancel", session)
        [request] = session.added
        engine, client = trading.Executor(), Client()
        async def unavailable(*args):
            raise ExchangeError(408, -1007, "结果未知")
        monkeypatch.setattr(client, "open_orders", unavailable)
        try:
            with pytest.raises(ExchangeError) as error:
                await engine.reconcile(session, client, SimpleNamespace(symbol="BTCUSDT"), True, request)
            assert error.value.code == -1007
            assert request.kind == "manual_requested" and request.data == {"action": "cancel"}
            assert not any(e.kind == "manual_completed" for e in session.added)
            async def empty(*args):
                return []
            monkeypatch.setattr(client, "open_orders", empty)
            recovered = Session(None, [[], []])
            await engine.reconcile(recovered, client, SimpleNamespace(symbol="BTCUSDT"), True, request)
            [completed] = recovered.added
            assert completed.kind == "manual_completed" and completed.data == {"request_id": request.id}
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


def test_replacement_proposal_survives_old_plan_cancellation(trading):
    async def check():
        engine, client = trading.Executor(), Client()
        current, proposal = plan(trading), plan(trading)
        proposal.id, proposal.action, proposal.plan_status = 4, "plan", "proposed"
        proposal.decision = {"valid_hours": 1}
        proposal.sizing["entry"]["stop_pct"] = "1"
        symbol = SimpleNamespace(symbol="BTCUSDT")
        try:
            assert await engine.adopt(Session(groups=[[proposal]]), client, symbol, current, None) is current
            assert current.plan_status == "closing" and proposal.plan_status == "proposed"
            assert await engine.adopt(Session(), client, symbol, current, None) is current
            assert proposal.plan_status == "proposed"
            assert await engine.adopt(Session(groups=[[proposal]]), client, symbol, None, None) is proposal
            assert proposal.plan_status == "pending"
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("closed,mark,rejected", [("100", "110", False), ("110", "100", True)])
@pytest.mark.parametrize("last_bar_closed", [False, True])
def test_submit_reevaluate_uses_latest_closed_candle(trading, monkeypatch, closed, mark, rejected, last_bar_closed):
    from alphapulse.trading import analyst
    from alphapulse.trading.plan import PlanContext

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    rows = [[0, "0", "0", "0", closed, "0", now_ms - 1000],
            [1, "0", "0", "0", mark, "0", now_ms + 60_000]]
    if last_bar_closed:
        rows[-1][4], rows[-1][6] = closed, now_ms - 1
    monkeypatch.setattr(analyst.market_client, "get", AsyncMock(return_value=rows))
    monkeypatch.setattr(analyst, "mark_price", AsyncMock(return_value=Decimal(mark)))
    monkeypatch.setattr(analyst, "recent_atr", AsyncMock(return_value=Decimal(1)))
    monkeypatch.setattr(analyst, "read_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(analyst, "add_event", AsyncMock())
    ctx = PlanContext(Decimal(mark), Decimal(1000), RULES, Decimal(1), False, None)
    prepared = SimpleNamespace(exchange="bybit", context=ctx)

    async def check():
        result, outcome = await analyst.submit(prepared, "BTCUSDT", 1, {
            "action": "wait", "summary": "s", "reasons": "r", "risks": "x",
            "reevaluate": [{"direction": "above", "price": "105", "timeframe": "5m", "basis": "收盘确认"}],
        })
        assert ctx.closed_prices == {"5m": Decimal(closed)}
        assert (outcome is None) == rejected
        assert ("已经满足" in result) == rejected
    asyncio.run(check())
