from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pytest

from alphapulse.trading.plan import (
    OrderSpec, PlanContext, PlanError, SymbolRules, TradePlan, desired_orders, diff_orders, next_routine_at, round_to_step,
    size_plan, split_take_profits,
)

BTC = SymbolRules(tick_size=Decimal("0.1"), step_size=Decimal("0.0001"), min_notional=Decimal("50"))


def context(**overrides) -> PlanContext:
    values = {
        "angle": "long", "mark_price": Decimal("82000"), "capital": Decimal("1000"), "other_notional": Decimal("0"),
        "rules": BTC, "has_plan": False, "position_side": None,
    }
    return PlanContext(**{**values, **overrides})


def long_plan(**overrides) -> TradePlan:
    values = {
        "action": "plan", "summary": "长线做多", "reasons": "支撑区", "risks": "无",
        "direction": "long", "confidence": "medium",
        "entries": [{"price": "81000", "ratio": "0.4", "basis": "区1"}, {"price": "80000", "ratio": "0.6", "basis": "区2"}],
        "stop": "79500", "stop_basis": "区下沿外",
        "take_profits": [{"price": "83000", "basis": "压1"}, {"price": "84000", "basis": "压2"}, {"price": "85000", "basis": "压3"}],
        "entry_valid_hours": "24", "max_hold_hours": "48",
        "invalidation": {"direction": "below", "price": "79800", "timeframe": "4h", "basis": "跌破区"},
    }
    return TradePlan.model_validate({**values, **overrides})


def short_term_plan(**overrides) -> TradePlan:
    values = {
        "action": "plan", "summary": "短线做空", "reasons": "压力区", "risks": "无",
        "direction": "short", "confidence": "medium",
        "entries": [{"price": "82500", "ratio": "1", "basis": "压力区"}],
        "stop": "82900", "take_profits": [{"price": "81800", "basis": "支1"}, {"price": "81300", "basis": "支2"}],
        "entry_valid_hours": "4", "max_hold_hours": "24",
        "invalidation": {"direction": "above", "price": "82800", "timeframe": "15m", "basis": "站上区"},
    }
    return TradePlan.model_validate({**values, **overrides})


def errors_of(plan: TradePlan, ctx: PlanContext) -> str:
    with pytest.raises(PlanError) as e:
        size_plan(plan, ctx)
    return "；".join(e.value.errors)


def test_round_to_step():
    assert round_to_step(Decimal("81000.05"), Decimal("0.1"), ROUND_FLOOR) == Decimal("81000.0")
    # 整数输入不能变成指数形式
    assert str(round_to_step(Decimal("80500"), Decimal("0.1"), ROUND_FLOOR)) == "80500.0"
    assert round_to_step(Decimal("82500.01"), Decimal("0.1"), ROUND_CEILING) == Decimal("82500.1")
    assert round_to_step(Decimal("0.00829908"), Decimal("0.0001"), ROUND_FLOOR) == Decimal("0.0082")


def test_split_take_profits_keeps_total():
    parts = split_take_profits(Decimal("0.0206"), (Decimal("0.5"), Decimal("0.3"), Decimal("0.2")), Decimal("0.0001"))
    assert parts == [Decimal("0.0103"), Decimal("0.0061"), Decimal("0.0042")]
    assert sum(parts) == Decimal("0.0206")
    assert split_take_profits(Decimal("0.001"), (Decimal("0.5"), Decimal("0.3"), Decimal("0.2")), Decimal("0.001")) == [0, 0, Decimal("0.001")]


def test_long_plan_sizing_matches_hand_calculation():
    sizing = size_plan(long_plan(), context())
    # 均价 0.4×81000 + 0.6×80000 = 80400；单位亏损 900 + (80400+79500)×0.0004 = 963.96；20 ÷ 963.96 = 0.020747
    assert sizing["average_price"] == "80400.0"
    assert [e["quantity"] for e in sizing["entries"]] == ["0.0082", "0.0124"]
    assert sizing["quantity"] == "0.0206"
    assert sizing["notional"] == "1656.20"
    assert sizing["risk_amount"] == "20.00"
    assert sizing["risk_at_stop"] == "19.82"
    # (0.5×2600 + 0.3×3600 + 0.2×4600) ÷ 900；只成交首档 (0.5×2000 + 0.3×3000 + 0.2×4000) ÷ 1500
    assert sizing["reward_risk"] == "3.67"
    assert sizing["reward_risk_first_only"] == "1.80"
    assert [t["quantity"] for t in sizing["take_profits"]] == ["0.0103", "0.0061", "0.0042"]
    assert sizing["capped_by_leverage"] is False


def test_low_confidence_halves_risk():
    sizing = size_plan(long_plan(confidence="low"), context())
    assert sizing["risk_amount"] == "10.00"
    # 10 ÷ 963.96 = 0.010374：0.4 档 0.0041，0.6 档 0.0062
    assert sizing["quantity"] == "0.0103"


def test_leverage_cap_counts_other_positions():
    sizing = size_plan(long_plan(), context(other_notional=Decimal("2000")))
    assert sizing["capped_by_leverage"] is True
    assert Decimal(sizing["notional"]) <= Decimal("1000")


def test_short_term_short_plan():
    sizing = size_plan(short_term_plan(), context(angle="short"))
    # 止盈 1 盈亏比 700 ÷ 400
    assert sizing["reward_risk"] == "1.75"
    assert sizing["risk_amount"] == "10.00"
    assert len(sizing["take_profits"]) == 2


def test_short_prices_round_up():
    sizing = size_plan(short_term_plan(entries=[{"price": "82500.01", "ratio": "1", "basis": "x"}]), context(angle="short"))
    assert sizing["entries"][0]["price"] == "82500.1"


def test_rejects_wrong_counts_and_timeframe():
    message = errors_of(short_term_plan(entries=[{"price": "82500", "ratio": "0.5", "basis": "a"}, {"price": "82600", "ratio": "0.5", "basis": "b"}]), context(angle="short"))
    assert "短线只能 1 档入场" in message
    assert "长线失效条件的周期只能是 4h" in errors_of(long_plan(invalidation={"direction": "below", "price": "79800", "timeframe": "1h", "basis": "x"}), context())
    assert "长线须给 3 个止盈" in errors_of(long_plan(take_profits=[{"price": "83000", "basis": "a"}]), context())


def test_rejects_bad_price_order():
    message = errors_of(long_plan(stop="80500"), context())
    assert "做多时止损 80500.0 应在失效价 79800 之外" in message
    assert "止盈 1 81000.0 已越过现价" in errors_of(long_plan(take_profits=[{"price": "81000", "basis": "a"}, {"price": "84000", "basis": "b"}, {"price": "85000", "basis": "c"}], entries=[{"price": "80500", "ratio": "1", "basis": "a"}]), context())


def test_rejects_low_reward_risk():
    # (0.5×400 + 0.3×600 + 0.2×800) ÷ 500 = 1.08
    plan = long_plan(take_profits=[{"price": "80400", "basis": "a"}, {"price": "80600", "basis": "b"}, {"price": "80800", "basis": "c"}],
                     entries=[{"price": "80000", "ratio": "1", "basis": "a"}])
    assert "盈亏比 1.08" in errors_of(plan, context(mark_price=Decimal("80300")))


def test_rejects_entries_below_min_notional():
    message = errors_of(long_plan(entries=[{"price": "81000", "ratio": "0.02", "basis": "a"}, {"price": "80000", "ratio": "0.98", "basis": "b"}]), context())
    assert "最小下单金额 50" in message
    assert "第 1 档" in message


def test_action_rules():
    assert "不能用 keep" in errors_of(TradePlan(action="keep", summary="s", reasons="r", risks="x"), context())
    assert "不能用 wait" in errors_of(TradePlan(action="wait", summary="s", reasons="r", risks="x"), context(position_side="long"))
    assert size_plan(TradePlan(action="wait", summary="s", reasons="r", risks="x"), context()) == {}
    satisfied = TradePlan(action="wait", summary="s", reasons="r", risks="x",
                          reevaluate=[{"direction": "above", "price": "81000", "timeframe": "1h", "basis": "x"}])
    assert "已经满足" in errors_of(satisfied, context())
    assert "方向必须一致" in errors_of(long_plan(), context(position_side="short"))


# ---------- 有持仓时的新计划 ----------

def managed_plan(**overrides) -> TradePlan:
    values = {
        "action": "plan", "summary": "调整", "reasons": "r", "risks": "x", "direction": "long", "confidence": "medium",
        "stop": "80000", "take_profits": [{"price": "83000", "basis": "a"}, {"price": "84000", "basis": "b"}, {"price": "85000", "basis": "c"}],
        "invalidation": {"direction": "below", "price": "80500", "timeframe": "4h", "basis": "x"},
    }
    return TradePlan.model_validate({**values, **overrides})


def holding(**overrides) -> PlanContext:
    return context(position_side="long", position_qty=Decimal("0.0100"), position_entry=Decimal("81000"), current_stop=Decimal("79500"), has_plan=True, **overrides)


def test_managed_plan_does_not_add_and_splits_position():
    sizing = size_plan(managed_plan(), holding())
    assert sizing["managed"] is True
    assert sizing["entries"] == []
    assert [t["quantity"] for t in sizing["take_profits"]] == ["0.0050", "0.0030", "0.0020"]
    assert sizing["position_qty"] == "0.0100"
    assert "entries 须为空" in errors_of(managed_plan(entries=[{"price": "80000", "ratio": "1", "basis": "a"}]), holding())


def test_managed_plan_only_tightens_stop():
    assert "止损只能收紧" in errors_of(managed_plan(stop="79000", invalidation={"direction": "below", "price": "79400", "timeframe": "4h", "basis": "x"}), holding())


# ---------- 应有订单 ----------

LONG_SIZING = {
    "direction": "long", "managed": False, "quantity": "0.0030",
    "entries": [{"price": "81000.0", "quantity": "0.0012"}, {"price": "80000.0", "quantity": "0.0018"}],
    "take_profits": [{"price": "83000.0", "ratio": "0.5"}, {"price": "84000.0", "ratio": "0.3"}, {"price": "85000.0", "ratio": "0.2"}],
}
ZERO3 = [Decimal(0)] * 3


def test_desired_orders_before_fill_places_entries_and_stop_together():
    orders = desired_orders(LONG_SIZING, entries_open=True, entry_filled=[Decimal(0)] * 2, tp_filled=ZERO3,
                            position_qty=Decimal(0), stop_price=Decimal("79500"), step_size=Decimal("0.0001"))
    assert [(o.purpose, o.leg, o.side, o.quantity) for o in orders] == [
        ("entry", 0, "BUY", Decimal("0.0012")), ("entry", 1, "BUY", Decimal("0.0018")), ("stop", None, "SELL", Decimal("0.0030")),
    ]


def test_desired_orders_partial_fill_splits_take_profits_by_filled():
    orders = desired_orders(LONG_SIZING, entries_open=True, entry_filled=[Decimal("0.0012"), Decimal("0.0004")], tp_filled=ZERO3,
                            position_qty=Decimal("0.0016"), stop_price=Decimal("79500"), step_size=Decimal("0.0001"))
    assert [(o.purpose, o.leg, o.quantity) for o in orders] == [
        ("entry", 1, Decimal("0.0014")), ("stop", None, Decimal("0.0030")),
        ("tp", 0, Decimal("0.0008")), ("tp", 1, Decimal("0.0004")), ("tp", 2, Decimal("0.0004")),
    ]


def test_desired_orders_after_tp1_keeps_remaining_targets_only():
    orders = desired_orders(LONG_SIZING, entries_open=False, entry_filled=[Decimal("0.0012"), Decimal("0.0018")],
                            tp_filled=[Decimal("0.0015"), Decimal(0), Decimal(0)], position_qty=Decimal("0.0015"),
                            stop_price=Decimal("81050"), step_size=Decimal("0.0001"))
    assert [(o.purpose, o.leg, o.quantity, o.trigger_price) for o in orders] == [
        ("stop", None, Decimal("0.0030"), Decimal("81050")), ("tp", 1, Decimal("0.0009"), None), ("tp", 2, Decimal("0.0006"), None),
    ]


def test_desired_orders_nothing_when_flat_and_entries_closed():
    assert desired_orders(LONG_SIZING, entries_open=False, entry_filled=[Decimal(0)] * 2, tp_filled=ZERO3,
                          position_qty=Decimal(0), stop_price=Decimal("79500"), step_size=Decimal("0.0001")) == []


def test_diff_orders_matches_by_value():
    keep = OrderSpec("stop", None, "SELL", "STOP_MARKET", None, Decimal("79500.0"), Decimal("0.0030"))
    stale = OrderSpec("entry", 0, "BUY", "LIMIT", Decimal("81000"), None, Decimal("0.0012"))
    wanted = OrderSpec("entry", 0, "BUY", "LIMIT", Decimal("80900"), None, Decimal("0.0012"))
    cancel, place = diff_orders([OrderSpec("stop", None, "SELL", "STOP_MARKET", None, Decimal("79500"), Decimal("0.003")), wanted],
                                [(1, keep), (2, stale)])
    assert cancel == [2]
    assert place == [wanted]


def test_next_routine_at_aligns_to_candle_close():
    tz8 = timezone(timedelta(hours=8))
    # 北京 10:20 = UTC 02:20；短线 +45 分钟后的第一个整点 UTC 04:00，再加 1 分钟
    assert next_routine_at(datetime(2026, 10, 15, 10, 20, tzinfo=tz8), "short") == datetime(2026, 10, 15, 4, 1, tzinfo=UTC)
    assert next_routine_at(datetime(2026, 10, 15, 3, 1, tzinfo=UTC), "short") == datetime(2026, 10, 15, 4, 1, tzinfo=UTC)
    # 长线：UTC 04:01 + 3h = 07:01，下一个 4h 收盘 08:00
    assert next_routine_at(datetime(2026, 10, 15, 4, 1, tzinfo=UTC), "long") == datetime(2026, 10, 15, 8, 1, tzinfo=UTC)


def test_step_decimal_never_uses_exponent():
    from alphapulse.trading.client import step_decimal

    assert str(step_decimal("0.10")) == "0.1"
    assert str(step_decimal("100")) == "100"
    assert str(round_to_step(Decimal("12345"), step_decimal("100"), ROUND_FLOOR)) == "12300"
