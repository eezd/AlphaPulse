from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pytest

from alphapulse.trading.plan import (
    Bar, OrderSpec, PlanContext, PlanError, SymbolRules, TradePlan, atr, check_setup, desired_orders, diff_orders, next_routine_at,
    round_to_step, scan_setup, setup_stop, size_entry, split_take_profits, take_profit_targets,
)

BTC = SymbolRules(tick_size=Decimal("0.1"), step_size=Decimal("0.0001"), min_notional=Decimal("50"))
TZ8 = timezone(timedelta(hours=8))
FIVE_MINUTES_MS = 5 * 60_000

# BTCUSDT 永续 5m，2026-10-15 北京时间（开盘时间, 最高, 最低, 收盘），即用户举例的那段行情
BTC_BARS = [
    ("09:10", "81858.10", "81629.20", "81687.60"), ("09:15", "81771.40", "81570.20", "81716.40"),
    ("09:20", "81732.40", "81584.80", "81584.90"), ("09:25", "81615.70", "81451.60", "81544.00"),
    ("09:30", "81810.00", "81523.80", "81751.80"), ("09:35", "81827.40", "81675.00", "81731.70"),
    ("09:40", "81832.80", "81700.80", "81809.50"), ("09:45", "81810.00", "81637.20", "81680.20"),
    ("09:50", "81795.00", "81656.70", "81678.90"), ("09:55", "81730.00", "81575.10", "81602.00"),
    ("10:00", "81619.40", "81495.20", "81507.10"), ("10:05", "81737.00", "81500.00", "81554.70"),
    ("10:10", "81620.00", "81412.80", "81412.80"), ("10:15", "81775.10", "81406.20", "81769.20"),
    ("10:20", "81770.10", "80664.20", "80700.10"), ("10:25", "81038.10", "80188.00", "80804.20"),
    ("10:30", "81057.50", "80476.70", "80979.30"), ("10:35", "81293.20", "80959.00", "81081.90"),
    ("10:40", "81293.00", "81053.30", "81293.00"), ("10:45", "81350.00", "81111.00", "81213.70"),
    ("10:50", "81411.70", "81152.70", "81410.00"), ("10:55", "81646.50", "81409.90", "81500.00"),
    ("11:00", "81514.40", "81077.70", "81153.30"), ("11:05", "81332.10", "81116.00", "81288.10"),
]


def ms(hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return int(datetime(2026, 10, 15, hour, minute, tzinfo=TZ8).timestamp() * 1000)


def bars_until(hhmm: str) -> list[Bar]:
    """到 hhmm 为止的 K 线，最后一根是 hhmm 开盘、尚未收盘的那根"""
    return [Bar(ms(t), ms(t) + FIVE_MINUTES_MS - 1, Decimal(h), Decimal(lo), Decimal(c)) for t, h, lo, c in BTC_BARS if ms(t) <= ms(hhmm)]


def scan(direction, zone_low, zone_high, confirm, adopted: str, now: str):
    setup = {"zone_low": zone_low, "zone_high": zone_high, "confirm_price": confirm}
    return scan_setup(direction, setup, bars_until(now), ms(adopted), ms(now) + 2000)


def context(**overrides) -> PlanContext:
    values = {
        "mark_price": Decimal("81000"), "capital": Decimal("1000"), "other_notional": Decimal("0"), "rules": BTC,
        "atr": Decimal("100"), "has_plan": False, "position_side": None,
    }
    return PlanContext(**{**values, **overrides})


def setup_plan(**overrides) -> TradePlan:
    values = {
        "action": "plan", "summary": "回踩支撑做多", "reasons": "支撑区", "risks": "无", "direction": "long", "confidence": "medium",
        "setup": {"zone_low": "80200", "zone_high": "80700", "confirm_price": "80750", "basis": "1h 支撑区"}, "valid_hours": "2",
    }
    return TradePlan.model_validate({**values, **overrides})


def errors_of(plan: TradePlan, ctx: PlanContext) -> str:
    with pytest.raises(PlanError) as e:
        check_setup(plan, ctx)
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


# ---------- 5m 确认（用户的两个例子） ----------

def test_long_example_triggers_on_close_back_above_after_dip():
    # 10:20 收在 80700 还低于确认价；10:25 下探到 80188（影线低于区间下沿但收盘没破）收回 80804 → 10:30 触发
    result = scan("long", "80200", "80700", "80750", adopted="10:12", now="10:30")
    assert result.state == "triggered"
    assert result.bar.open_time == ms("10:25")
    assert result.extreme == Decimal("80188.00")
    assert scan("long", "80200", "80700", "80750", adopted="10:12", now="10:25").state == "waiting"


def test_short_example_triggers_on_rejection_from_resistance():
    # 10:50 只到 81411.7，未进区；10:55 冲到 81646.5 后收在 81500 → 11:00 触发
    result = scan("short", "81500", "81700", "81500", adopted="10:47", now="11:00")
    assert (result.state, result.bar.open_time, result.extreme) == ("triggered", ms("10:55"), Decimal("81646.50"))


def test_bars_closed_before_adoption_are_ignored():
    # 10:25 那根在计划生效（10:30）前已收盘，不算
    assert scan("long", "80200", "80700", "80750", adopted="10:30", now="10:30").state == "waiting"


def test_trigger_not_on_latest_bar_is_missed():
    assert scan("long", "80200", "80700", "80750", adopted="10:12", now="10:35").state == "missed"


def test_close_beyond_far_edge_breaks_setup():
    result = scan("long", "80750", "81000", "81050", adopted="10:12", now="10:30")
    assert (result.state, result.bar.open_time) == ("broken", ms("10:20"))


def test_atr_and_stop_from_example():
    closed = bars_until("10:25")
    value = atr(closed)
    assert value.quantize(Decimal("0.0001")) == Decimal("302.9714")
    # 做多：80188 − 0.5 × 302.97 = 80036.51 → 向下取整 80036.5，距 80804.2 约 0.95%，仍在 1% 以内
    stop = setup_stop("long", Decimal("80188.00"), value, BTC.tick_size)
    assert stop == Decimal("80036.5")
    entry = size_entry("long", Decimal("80804.2"), stop, capital=Decimal("1000"), other_notional=Decimal("0"), confidence="medium", rules=BTC)
    assert entry["stop_pct"] == "0.95"
    # 10 ÷ (767.7 + (80804.2 + 80036.5) × 0.0004 = 832.04) = 0.01201… → 0.0120
    assert entry["quantity"] == "0.0120"
    assert setup_stop("short", Decimal("81646.50"), atr(bars_until("10:55")), BTC.tick_size) == Decimal("81827.8")


def test_size_entry_rejects_far_stop_and_halves_low_confidence():
    with pytest.raises(PlanError, match="超过 1%"):
        size_entry("long", Decimal("80000"), Decimal("79100"), capital=Decimal("1000"), other_notional=Decimal("0"), confidence="high", rules=BTC)
    with pytest.raises(PlanError, match="越过现价"):
        size_entry("short", Decimal("80000"), Decimal("79900"), capital=Decimal("1000"), other_notional=Decimal("0"), confidence="high", rules=BTC)
    medium = size_entry("long", Decimal("80000"), Decimal("79600"), capital=Decimal("1000"), other_notional=Decimal("0"), confidence="medium", rules=BTC)
    low = size_entry("long", Decimal("80000"), Decimal("79600"), capital=Decimal("1000"), other_notional=Decimal("0"), confidence="low", rules=BTC)
    assert (medium["risk_amount"], low["risk_amount"]) == ("10.00", "5.00")
    # 止损很近时按 3 倍杠杆封顶：3000 − 其他币 1000 = 2000 ÷ 80000 = 0.025
    capped = size_entry("long", Decimal("80000"), Decimal("79990"), capital=Decimal("1000"), other_notional=Decimal("1000"), confidence="high", rules=BTC)
    assert (capped["quantity"], capped["capped_by_leverage"]) == ("0.0250", True)


def test_take_profit_ladders():
    long = take_profit_targets("long", Decimal("80804.2"), Decimal("0.0120"), BTC)
    # +0.5% = 81208.22 → 81208.2；+0.75% = 81410.23 → 81410.2；+1% = 81612.24 → 81612.2；数量 60/30/10
    assert long == [(Decimal("81208.2"), Decimal("0.0072")), (Decimal("81410.2"), Decimal("0.0036")), (Decimal("81612.2"), Decimal("0.0012"))]
    short = take_profit_targets("short", Decimal("81500"), Decimal("0.0123"), BTC)
    # −0.5% = 81092.5；−0.75% = 80888.75 → 向上取整 80888.8；数量 80/20
    assert short == [(Decimal("81092.5"), Decimal("0.0098")), (Decimal("80888.8"), Decimal("0.0025"))]


# ---------- 提交校验 ----------

def test_check_setup_accepts_and_estimates():
    sizing = check_setup(setup_plan(), context())
    assert sizing["setup"] == {"zone_low": "80200", "zone_high": "80700", "confirm_price": "80750"}
    # 预估止损 = 80200 − 0.5 × 100 = 80150，距确认价 80750 约 0.74%
    assert (sizing["estimate"]["stop"], sizing["estimate"]["stop_pct"]) == ("80150.0", "0.74")
    assert sizing["position_qty"] == "0"


def test_check_setup_rejects_bad_setups():
    bad_order = setup_plan(setup={"zone_low": "80700", "zone_high": "80200", "confirm_price": "80750", "basis": "x"})
    assert "须低于上沿" in errors_of(bad_order, context())
    inside = setup_plan(setup={"zone_low": "80200", "zone_high": "80700", "confirm_price": "80500", "basis": "x"})
    assert "不低于区间上沿" in errors_of(inside, context())
    short_inside = setup_plan(direction="short", setup={"zone_low": "81500", "zone_high": "81700", "confirm_price": "81600", "basis": "x"})
    assert "不高于区间下沿" in errors_of(short_inside, context())
    assert "支撑已破" in errors_of(setup_plan(), context(mark_price=Decimal("80100")))
    wide = setup_plan(setup={"zone_low": "79800", "zone_high": "80700", "confirm_price": "80750", "basis": "x"})
    assert "超过 1%" in errors_of(wide, context())
    assert "valid_hours" in errors_of(setup_plan(valid_hours="3"), context())


def test_action_rules():
    assert "不能用 keep" in errors_of(TradePlan(action="keep", summary="s", reasons="r", risks="x"), context())
    assert "不能用 wait" in errors_of(TradePlan(action="wait", summary="s", reasons="r", risks="x"), context(position_side="long"))
    assert check_setup(TradePlan(action="wait", summary="s", reasons="r", risks="x"), context()) == {}
    satisfied = TradePlan(action="wait", summary="s", reasons="r", risks="x",
                          reevaluate=[{"direction": "above", "price": "80000", "timeframe": "5m", "basis": "x"}])
    assert "已经满足" in errors_of(satisfied, context())
    assert "只能 keep 或 exit" in errors_of(setup_plan(), context(position_side="long", has_plan=True))


# ---------- 应有订单 ----------

TRIGGERED = {"direction": "long", "setup": {}, "entry": {"quantity": "0.0120", "stop": "80036.5"}}
NO_TP = [Decimal(0)] * 3


def test_desired_orders_places_stop_and_market_entry_once():
    orders = desired_orders(TRIGGERED, entry_sent=False, entry_fill=None, tp_filled=NO_TP, position_qty=Decimal(0),
                            stop_price=Decimal("80036.5"), rules=BTC)
    assert [(o.purpose, o.side, o.type, o.quantity) for o in orders] == [
        ("entry", "BUY", "MARKET", Decimal("0.0120")), ("stop", "SELL", "STOP_MARKET", Decimal("0.0120")),
    ]
    # 入场单已发出、还没成交：只保留止损，不重发入场
    orders = desired_orders(TRIGGERED, entry_sent=True, entry_fill=None, tp_filled=NO_TP, position_qty=Decimal(0),
                            stop_price=Decimal("80036.5"), rules=BTC)
    assert [o.purpose for o in orders] == ["stop"]


def test_desired_orders_take_profits_follow_fill_price():
    orders = desired_orders(TRIGGERED, entry_sent=True, entry_fill=(Decimal("0.0120"), Decimal("80810.0")),
                            tp_filled=[Decimal("0.0072"), Decimal(0), Decimal(0)], position_qty=Decimal("0.0048"),
                            stop_price=Decimal("80850.0"), rules=BTC)
    assert [(o.purpose, o.leg, o.price, o.trigger_price, o.quantity) for o in orders] == [
        ("stop", None, None, Decimal("80850.0"), Decimal("0.0120")),
        ("tp", 1, Decimal("81416.0"), None, Decimal("0.0036")), ("tp", 2, Decimal("81618.1"), None, Decimal("0.0012")),
    ]


def test_desired_orders_nothing_after_flat():
    assert desired_orders(TRIGGERED, entry_sent=True, entry_fill=(Decimal("0.0120"), Decimal("80810.0")), tp_filled=NO_TP,
                          position_qty=Decimal(0), stop_price=Decimal("80036.5"), rules=BTC) == []


def test_diff_orders_matches_by_value():
    keep = OrderSpec("stop", None, "SELL", "STOP_MARKET", None, Decimal("79500.0"), Decimal("0.0030"))
    stale = OrderSpec("entry", 0, "BUY", "LIMIT", Decimal("81000"), None, Decimal("0.0012"))
    wanted = OrderSpec("entry", 0, "BUY", "LIMIT", Decimal("80900"), None, Decimal("0.0012"))
    cancel, place = diff_orders([OrderSpec("stop", None, "SELL", "STOP_MARKET", None, Decimal("79500"), Decimal("0.003")), wanted],
                                [(1, keep), (2, stale)])
    assert cancel == [2]
    assert place == [wanted]


def test_next_routine_at_aligns_to_candle_close():
    # 北京 10:20 = UTC 02:20；+45 分钟后的第一个整点 UTC 04:00，再加 1 分钟
    assert next_routine_at(datetime(2026, 10, 15, 10, 20, tzinfo=TZ8)) == datetime(2026, 10, 15, 4, 1, tzinfo=UTC)
    assert next_routine_at(datetime(2026, 10, 15, 3, 1, tzinfo=UTC)) == datetime(2026, 10, 15, 4, 1, tzinfo=UTC)


def test_step_decimal_never_uses_exponent():
    from alphapulse.trading.client import step_decimal

    assert str(step_decimal("0.10")) == "0.1"
    assert str(step_decimal("100")) == "100"
    assert str(round_to_step(Decimal("12345"), step_decimal("100"), ROUND_FLOOR)) == "12300"
