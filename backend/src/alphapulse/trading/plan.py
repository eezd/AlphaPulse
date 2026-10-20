"""交易计划的纯计算：模型提交的区间和限价、校验、止损与仓位、止盈阶梯、应有订单。

只做短线做波动：模型划出支撑区（压力区）并给出区内的限价，采纳后立即挂限价单（maker 手续费）；
止损放在区间远端外（且离挂单价至少 1×ATR），与入场单一起挂；止盈按成交均价的固定比例。
下方常量与 main/README 的数字重复，并写进了分析器的提示词；改 README 时要同步修改。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Direction = Literal["long", "short"]

RISK_PCT = Decimal("0.01")
LOW_CONFIDENCE_FACTOR = Decimal("0.5")
MAX_LEVERAGE = 3
# 限价单的最长有效小时数；首次成交后的最长持仓小时数
SETUP_VALID_HOURS = 2
MAX_HOLD_HOURS = 4
ATR_INTERVAL = "5m"
ATR_PERIOD = 14
# 止损放在区间远端外 STOP_ATR_FACTOR × ATR14(5m)，且离挂单价至少 MIN_STOP_ATR × ATR：
# 挂单价贴近远端时，只按远端算的止损会落在 5m 正常波动里（#34 只有 0.11%）。离挂单价超过 MAX_STOP_PCT 的计划不做
STOP_ATR_FACTOR = Decimal("0.5")
MIN_STOP_ATR = Decimal("1")
MAX_STOP_PCT = Decimal("0.01")
# 止盈阶梯：（距成交均价的涨跌幅，平仓比例）。牛市里空单不恋战，第一档就平掉大部分
TAKE_PROFIT_STEPS: dict[str, tuple[tuple[Decimal, Decimal], ...]] = {
    "long": ((Decimal("0.005"), Decimal("0.6")), (Decimal("0.0075"), Decimal("0.3")), (Decimal("0.01"), Decimal("0.1"))),
    "short": ((Decimal("0.005"), Decimal("0.8")), (Decimal("0.0075"), Decimal("0.2"))),
}
MAX_REEVALUATE = 4
# 风险按吃单手续费保守估算（C1 实测 taker 0.04%；限价入场实际是 maker 0.02%）
TAKER_FEE_RATE = Decimal("0.0004")
# 例行分析周期：跟 1h 收盘
ROUTINE_HOURS = 1
# 同一个币两次自动分析的最短间隔
MIN_ANALYSIS_GAP = timedelta(minutes=10)

Price = Annotated[Decimal, Field(gt=0)]


class Condition(BaseModel):
    direction: Annotated[Literal["above", "below"], Field(description="above：价格高于 price 时满足；below：低于时满足")]
    price: Price
    timeframe: Annotated[
        Literal["mark", "1m", "5m", "15m", "1h", "4h"],
        Field(description="mark：标记价触及即满足；其他：该周期 K 线收盘价满足才算"),
    ]
    basis: Annotated[str, Field(description="依据，如对应的支撑区、压力区")]


class Setup(BaseModel):
    zone_low: Price
    zone_high: Price
    entry_price: Annotated[Decimal, Field(gt=0, description=(
        "限价单价格，须在区间内：做多低于现价，等价格回落成交；做空高于现价，等价格反弹成交"
    ))]
    basis: Annotated[str, Field(description="区间和挂单价的依据：对应的支撑区或压力区、周期、触碰次数")]


class TradePlan(BaseModel):
    """submit_trade_plan 的参数"""

    action: Annotated[Literal["plan", "keep", "exit", "wait"], Field(description=(
        "plan：给出新的限价计划（替换当前未成交的计划，仅在没有持仓时）；keep：当前计划或持仓不变；"
        "exit：结束当前计划，撤销未成交的限价单，有持仓则市价平仓；wait：观望，不给计划（仅在没有持仓和当前计划时）"
    ))]
    summary: Annotated[str, Field(description="一句话结论，含方向和置信度")]
    reasons: Annotated[str, Field(description="依据要点：新闻、结构、衍生品，引用新闻写 [新闻 id]")]
    risks: Annotated[str, Field(description="有效期和持仓时限内的重大事件及时间、与结论相反的信号")]
    reevaluate: Annotated[list[Condition], Field(max_length=MAX_REEVALUATE, description=(
        "满足任一条件时程序重新请你分析：用来表达另一侧方向、需要重新划区的位置。提交时不能已经满足"
    ))] = []
    direction: Direction | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    setup: Annotated[Setup | None, Field(description="做多为支撑区，做空为压力区，以及区内的挂单价")] = None
    valid_hours: Annotated[Decimal | None, Field(gt=0, description=f"限价单有效小时数（≤ {SETUP_VALID_HOURS}），到期未成交则撤单")] = None


@dataclass
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal


@dataclass
class PlanContext:
    mark_price: Decimal
    # 该币的 min(交易资金, 账户权益)
    capital: Decimal
    rules: SymbolRules
    # 最近已收盘 5m K 线的 ATR14，用于计算止损
    atr: Decimal
    has_plan: bool
    position_side: Direction | None


@dataclass(frozen=True)
class Bar:
    open_time: int
    # 币安的 close_time：收盘前 1 毫秒
    close_time: int
    high: Decimal
    low: Decimal
    close: Decimal


class PlanError(Exception):
    """计划不通过校验；errors 逐条返回给模型修正"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("；".join(errors))
        self.errors = errors


def round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    # quantize 统一小数位，否则可能得到 8.05E+4 这类指数形式，转成字符串下单会被拒
    return ((value / step).to_integral_value(rounding) * step).quantize(step)


def split_take_profits(quantity: Decimal, ratios: tuple[Decimal, ...], step: Decimal) -> list[Decimal]:
    """按比例拆分止盈数量：前几档向下取整，最后一档取余数，保证合计等于 quantity。取整后为 0 的档位由调用方跳过"""
    parts = [round_to_step(quantity * r, step, ROUND_FLOOR) for r in ratios[:-1]]
    return [*parts, quantity - sum(parts, Decimal(0))]


def parse_bars(rows: list[list]) -> list[Bar]:
    """币安 klines 原始行"""
    return [Bar(int(r[0]), int(r[6]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4])) for r in rows]


def atr(bars: list[Bar], period: int = ATR_PERIOD) -> Decimal:
    """最后 period 根的真实波幅简单平均，与 get_klines 的 atr14 口径一致"""
    if len(bars) < period + 1:
        raise ValueError(f"计算 ATR 需要至少 {period + 1} 根 K 线，现有 {len(bars)} 根")
    recent = bars[-period - 1:]
    ranges = [max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)) for prev, b in zip(recent, recent[1:], strict=False)]
    return sum(ranges, Decimal(0)) / period


def setup_stop(direction: Direction, entry: Decimal, far_edge: Decimal, atr_value: Decimal, tick: Decimal) -> Decimal:
    """止损 = 区间远端外 STOP_ATR_FACTOR × ATR 与挂单价外 MIN_STOP_ATR × ATR 中离挂单价更远的一个，向远离入场的方向取整"""
    if direction == "long":
        return round_to_step(min(far_edge - STOP_ATR_FACTOR * atr_value, entry - MIN_STOP_ATR * atr_value), tick, ROUND_FLOOR)
    return round_to_step(max(far_edge + STOP_ATR_FACTOR * atr_value, entry + MIN_STOP_ATR * atr_value), tick, ROUND_CEILING)


def size_entry(direction: Direction, price: Decimal, stop: Decimal, *, capital: Decimal, confidence: str, rules: SymbolRules) -> dict:
    """按入场价和止损算数量：单笔风险上限与该币资金的杠杆上限取小。止损离入场价太远、方向不对或数量不足时抛 PlanError"""
    sign = 1 if direction == "long" else -1
    stop_pct = sign * (price - stop) / price
    if stop_pct <= 0:
        raise PlanError([f"止损 {stop} 不在入场价 {price} 的亏损一侧"])
    if stop_pct > MAX_STOP_PCT:
        raise PlanError([f"止损 {stop} 距入场价 {price} {stop_pct * 100:.2f}%，超过 {MAX_STOP_PCT * 100:.0f}%"])
    unit_loss = abs(price - stop) + (price + stop) * TAKER_FEE_RATE
    risk_amount = capital * RISK_PCT * (LOW_CONFIDENCE_FACTOR if confidence == "low" else 1)
    max_notional = capital * MAX_LEVERAGE
    by_risk = risk_amount / unit_loss
    by_leverage = max_notional / price
    quantity = round_to_step(min(by_risk, by_leverage), rules.step_size, ROUND_FLOOR)
    notional = quantity * price
    if notional < rules.min_notional:
        raise PlanError([f"数量 {quantity} 的名义价值 {notional:.2f} USDT 低于最小下单金额 {rules.min_notional} USDT"])
    risk_at_stop = quantity * unit_loss
    return {
        "price": str(price),
        "stop": str(stop),
        "stop_pct": str((stop_pct * 100).quantize(Decimal("0.01"))),
        "quantity": str(quantity),
        "notional": str(notional.quantize(Decimal("0.01"))),
        "leverage": str((notional / capital).quantize(Decimal("0.01"))),
        "risk_amount": str(risk_amount.quantize(Decimal("0.01"))),
        "risk_at_stop": str(risk_at_stop.quantize(Decimal("0.01"))),
        "risk_pct": str((risk_at_stop / capital * 100).quantize(Decimal("0.01"))),
        "capped_by_leverage": by_leverage < by_risk,
    }


def take_profit_targets(direction: Direction, average: Decimal, quantity: Decimal, rules: SymbolRules) -> list[tuple[Decimal, Decimal]]:
    """止盈阶梯（价格，数量）：价格 = 成交均价 ×（1 ± 涨跌幅），向靠近均价的方向取整；数量按比例拆分"""
    steps = TAKE_PROFIT_STEPS[direction]
    quantities = split_take_profits(quantity, tuple(r for _, r in steps), rules.step_size)
    if direction == "long":
        prices = [round_to_step(average * (1 + pct), rules.tick_size, ROUND_FLOOR) for pct, _ in steps]
    else:
        prices = [round_to_step(average * (1 - pct), rules.tick_size, ROUND_CEILING) for pct, _ in steps]
    return list(zip(prices, quantities, strict=True))


def check_action(plan: TradePlan, ctx: PlanContext) -> list[str]:
    errors = []
    if plan.action == "keep" and not ctx.has_plan:
        errors.append("当前没有计划，不能用 keep")
    if plan.action == "exit" and not ctx.has_plan and ctx.position_side is None:
        errors.append("当前没有计划也没有持仓，不能用 exit，观望请用 wait")
    if plan.action == "wait" and (ctx.has_plan or ctx.position_side is not None):
        errors.append("有当前计划或持仓时不能用 wait：保留用 keep，结束用 exit")
    if plan.action == "plan" and ctx.position_side is not None:
        errors.append("已有持仓：持仓由程序按止损、止盈和持仓时限管理，只能 keep 或 exit")
    for i, c in enumerate(plan.reevaluate, 1):
        satisfied = ctx.mark_price > c.price if c.direction == "above" else ctx.mark_price < c.price
        if satisfied:
            errors.append(f"reevaluate 第 {i} 个条件（{c.direction} {c.price}）按现价 {ctx.mark_price} 已经满足，提交后会立即触发")
    return errors


def check_setup(plan: TradePlan, ctx: PlanContext) -> dict:
    """校验计划；action=plan 时算出限价、止损和数量。不通过时抛 PlanError"""
    errors = check_action(plan, ctx)
    if plan.action != "plan":
        if errors:
            raise PlanError(errors)
        return {}
    missing = [name for name in ("direction", "confidence", "setup", "valid_hours") if getattr(plan, name) is None]
    if missing:
        raise PlanError([*errors, f"action=plan 时缺少：{', '.join(missing)}"])
    direction, setup, mark, rules = plan.direction, plan.setup, ctx.mark_price, ctx.rules
    long = direction == "long"
    # 向保守方向取整：做多的挂单价向下、做空向上
    entry = round_to_step(setup.entry_price, rules.tick_size, ROUND_FLOOR if long else ROUND_CEILING)
    if setup.zone_low >= setup.zone_high:
        errors.append(f"区间下沿 {setup.zone_low} 须低于上沿 {setup.zone_high}")
    if not setup.zone_low <= entry <= setup.zone_high:
        errors.append(f"挂单价 {entry} 须在区间 {setup.zone_low}–{setup.zone_high} 内")
    # 限价单要挂在现价下方（做空在上方）等价格过来，否则会立即按市价成交，付吃单手续费
    if long and entry >= mark:
        errors.append(f"做多的挂单价 {entry} 须低于现价 {mark}")
    if not long and entry <= mark:
        errors.append(f"做空的挂单价 {entry} 须高于现价 {mark}")
    if plan.valid_hours > SETUP_VALID_HOURS:
        errors.append(f"valid_hours 不能超过 {SETUP_VALID_HOURS}")
    if errors:
        raise PlanError(errors)
    stop = setup_stop(direction, entry, setup.zone_low if long else setup.zone_high, ctx.atr, rules.tick_size)
    try:
        sizing = size_entry(direction, entry, stop, capital=ctx.capital, confidence=plan.confidence, rules=rules)
    except PlanError as e:
        raise PlanError([f"止损 {stop}（区间{'下沿' if long else '上沿'}外 {STOP_ATR_FACTOR}×ATR(5m)，且距挂单价至少 "
                         f"{MIN_STOP_ATR}×ATR(5m)，ATR={ctx.atr.quantize(rules.tick_size)}）：{e}。"
                         "区间太宽或挂单价离区间远端太远，请收窄区间或调整挂单价"]) from e
    return {
        "direction": direction,
        "setup": {"zone_low": str(setup.zone_low), "zone_high": str(setup.zone_high)},
        "entry": sizing,
        "atr": str(ctx.atr.quantize(rules.tick_size)),
        # 执行器采纳前核对：持仓变了说明计划基于过时状态
        "position_qty": "0",
    }


# ---------- 执行：应有订单 ----------

@dataclass(frozen=True)
class OrderSpec:
    """一张应有（或现有）的订单；quantity 为剩余未成交数量"""

    purpose: Literal["entry", "stop", "tp", "close"]
    leg: int | None
    side: Literal["BUY", "SELL"]
    type: Literal["LIMIT", "STOP_MARKET", "MARKET"]
    price: Decimal | None
    trigger_price: Decimal | None
    quantity: Decimal


def desired_orders(
    sizing: dict, *, entries_open: bool, entry_filled: Decimal, entry_average: Decimal | None, tp_filled: list[Decimal],
    position_qty: Decimal, stop_price: Decimal, rules: SymbolRules,
) -> list[OrderSpec]:
    """计划在当前成交状态下应挂的单。

    - 入场：entries_open 时一张限价单，数量为剩余未成交部分。
    - 止损：有持仓或有入场单时一张只减仓止损，数量取计划数量与持仓的较大者（大于持仓时交易所只平实际持仓）。
    - 止盈：有持仓时按已成交数量和成交均价排出阶梯，减去各档已成交。
    """
    direction = sizing["direction"]
    open_side, close_side = ("BUY", "SELL") if direction == "long" else ("SELL", "BUY")
    quantity = Decimal(sizing["entry"]["quantity"])
    orders = []
    if entries_open and entry_filled < quantity:
        orders.append(OrderSpec("entry", 0, open_side, "LIMIT", Decimal(sizing["entry"]["price"]), None, quantity - entry_filled))
    if position_qty > 0 or orders:
        orders.append(OrderSpec("stop", None, close_side, "STOP_MARKET", None, stop_price, max(quantity, position_qty)))
    if position_qty > 0 and entry_average is not None:
        for j, (price, target) in enumerate(take_profit_targets(direction, entry_average, entry_filled, rules)):
            remaining = target - tp_filled[j]
            if remaining > 0:
                orders.append(OrderSpec("tp", j, close_side, "LIMIT", price, None, remaining))
    return orders


def diff_orders[K](desired: list[OrderSpec], actual: list[tuple[K, OrderSpec]]) -> tuple[list[K], list[OrderSpec]]:
    """对比应有与现有订单：返回（要撤销的现有订单，要新挂的订单）。价格、数量完全一致才算同一张"""
    missing = list(desired)
    cancel = []
    for key, spec in actual:
        if spec in missing:
            missing.remove(spec)
        else:
            cancel.append(key)
    return cancel, missing


def condition_met(direction: Literal["above", "below"], threshold: Decimal, price: Decimal) -> bool:
    return price > threshold if direction == "above" else price < threshold


def next_routine_at(last_started: datetime) -> datetime:
    """例行分析时间：上次分析后约 3/4 个周期之后的第一个 K 线收盘，再加 1 分钟等收盘数据就绪。
    任何一次分析（包括触发式）都会让例行分析重新计时"""
    period = timedelta(hours=ROUTINE_HOURS)
    # 币安 K 线按 UTC 对齐
    earliest = (last_started + period * 3 / 4).astimezone(UTC)
    day = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = day + period * -(-(earliest - day) // period)
    return boundary + timedelta(minutes=1)
