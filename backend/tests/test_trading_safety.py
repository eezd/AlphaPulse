import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

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


def test_stop_replacement_waits_for_exchange_confirmation(trading):
    async def check():
        engine, client, session = trading.Executor(), Client(), Session()
        try:
            p, old = plan(trading), order(trading, "stop")
            p.plan_status = "open"
            position = trading.Position("long", Decimal(1), Decimal(100), Decimal(100), Decimal(101))
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


def test_rejected_replacement_does_not_cancel_existing_stop(trading, monkeypatch):
    async def reject(*args):
        raise ExchangeError(400, 110092, "止损被拒")
    async def check():
        engine, client, session = trading.Executor(), Client(), Session()
        monkeypatch.setattr(client, "place_order", reject)
        p = plan(trading)
        try:
            protected = await engine.sync_orders(session, client, p, [order(trading, "stop")],
                                                  trading.Position("long", Decimal(1), Decimal(100), Decimal(100), Decimal(101)), False, RULES, False)
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
            await engine.unmanaged(session, client, "BTCUSDT", trading.Position("long", Decimal(1), Decimal(100), Decimal(100), Decimal(101)),
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
