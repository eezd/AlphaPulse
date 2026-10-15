"""交易计划的纯计算：模型提交的待确认入场、校验、5m 确认扫描、触发时的止损与仓位、止盈阶梯。

只做短线做波动：模型划出区间和确认价，程序在 5m 收盘确认后市价入场，止损和止盈由程序按固定规则计算。
下方常量与 main/README 的数字重复，并写进了分析器的提示词；改 README 时要同步修改。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Direction = Literal["long", "short"]

# 所有币合计每天（北京时间）最多自动分析的次数
DAILY_ANALYSIS_LIMIT = 60
RISK_PCT = Decimal("0.01")
LOW_CONFIDENCE_FACTOR = Decimal("0.5")
MAX_LEVERAGE = 3
# 待确认入场的最长有效小时数；成交后的最长持仓小时数
SETUP_VALID_HOURS = 2
MAX_HOLD_HOURS = 4
CONFIRM_INTERVAL = "5m"
ATR_PERIOD = 14
# 止损放在确认过程的极值外 STOP_ATR_FACTOR × ATR14(5m)；离现价超过 MAX_STOP_PCT 就放弃这次入场
STOP_ATR_FACTOR = Decimal("0.5")
MAX_STOP_PCT = Decimal("0.01")
# 止盈阶梯：（距成交均价的涨跌幅，平仓比例）。牛市里空单不恋战，第一档就平掉大部分
TAKE_PROFIT_STEPS: dict[str, tuple[tuple[Decimal, Decimal], ...]] = {
    "long": ((Decimal("0.005"), Decimal("0.6")), (Decimal("0.0075"), Decimal("0.3")), (Decimal("0.01"), Decimal("0.1"))),
    "short": ((Decimal("0.005"), Decimal("0.8")), (Decimal("0.0075"), Decimal("0.2"))),
}
MAX_REEVALUATE = 4
# 入场（市价）和止损都按吃单手续费估算（C1 实测 taker 0.04%）
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
    confirm_price: Annotated[Decimal, Field(gt=0, description=(
        "做多：不低于 zone_high，进区后 5m 收盘 ≥ 此价入场；做空：不高于 zone_low，进区后 5m 收盘 ≤ 此价入场"
    ))]
    basis: Annotated[str, Field(description="区间和确认价的依据：对应的支撑区或压力区、周期、触碰次数")]


class TradePlan(BaseModel):
    """submit_trade_plan 的参数"""

    action: Annotated[Literal["plan", "keep", "exit", "wait"], Field(description=(
        "plan：给出新的待确认入场（替换当前未触发的计划，仅在没有持仓时）；keep：当前计划或持仓不变；"
        "exit：结束当前计划，有持仓则市价平仓；wait：观望，不给计划（仅在没有持仓和当前计划时）"
    ))]
    summary: Annotated[str, Field(description="一句话结论，含方向和置信度")]
    reasons: Annotated[str, Field(description="依据要点：新闻、结构、衍生品，引用新闻写 [新闻 id]")]
    risks: Annotated[str, Field(description="有效期和持仓时限内的重大事件及时间、与结论相反的信号")]
    reevaluate: Annotated[list[Condition], Field(max_length=MAX_REEVALUATE, description=(
        "满足任一条件时程序重新请你分析：用来表达另一侧方向、需要重新划区的位置。提交时不能已经满足"
    ))] = []
    direction: Direction | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    setup: Annotated[Setup | None, Field(description="待确认入场：做多为支撑区，做空为压力区")] = None
    valid_hours: Annotated[Decimal | None, Field(gt=0, description=f"有效小时数（≤ {SETUP_VALID_HOURS}），到期未触发则作废")] = None


@dataclass
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal


@dataclass
class PlanContext:
    mark_price: Decimal
    # min(设定的交易资金, 账户权益)
    capital: Decimal
    # 交易列表中其他币的持仓名义价值合计
    other_notional: Decimal
    rules: SymbolRules
    # 最近已收盘 5m K 线的 ATR14，用于预估止损
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


@dataclass(frozen=True)
class SetupScan:
    state: Literal["waiting", "triggered", "broken", "missed"]
    bar: Bar | None = None
    # 进区以来的极值：做多为最低价，做空为最高价
    extreme: Decimal | None = None


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


def scan_setup(direction: Direction, setup: dict, bars: list[Bar], after_ms: int, now_ms: int) -> SetupScan:
    """按计划生效后收盘的 5m K 线判断待确认入场（按时间顺序，先发生的为准）：

    - 做多：某根最低价进入区间（≤ zone_high）后，出现收盘 ≥ 确认价（可以是同一根）→ 触发；
      在此之前任何一根收盘 < zone_low → 支撑破位。做空镜像。
    - 触发的那根不是最新一根已收盘 K 线（如程序停过）→ missed，不补做。
    """
    zone_low, zone_high, confirm = (Decimal(setup[k]) for k in ("zone_low", "zone_high", "confirm_price"))
    closed = [b for b in bars if after_ms <= b.close_time < now_ms]
    long = direction == "long"
    extreme: Decimal | None = None
    for i, bar in enumerate(closed):
        if (bar.close < zone_low) if long else (bar.close > zone_high):
            return SetupScan("broken", bar)
        if extreme is None and ((bar.low <= zone_high) if long else (bar.high >= zone_low)):
            extreme = bar.low if long else bar.high
        if extreme is None:
            continue
        extreme = min(extreme, bar.low) if long else max(extreme, bar.high)
        if (bar.close >= confirm) if long else (bar.close <= confirm):
            return SetupScan("triggered" if i == len(closed) - 1 else "missed", bar, extreme)
    return SetupScan("waiting")


def setup_stop(direction: Direction, extreme: Decimal, atr_value: Decimal, tick: Decimal) -> Decimal:
    """止损 = 极值外 STOP_ATR_FACTOR × ATR，向远离现价的方向取整"""
    if direction == "long":
        return round_to_step(extreme - STOP_ATR_FACTOR * atr_value, tick, ROUND_FLOOR)
    return round_to_step(extreme + STOP_ATR_FACTOR * atr_value, tick, ROUND_CEILING)


def size_entry(direction: Direction, price: Decimal, stop: Decimal, *, capital: Decimal, other_notional: Decimal,
               confidence: str, rules: SymbolRules) -> dict:
    """按估计入场价和止损算数量：单笔风险上限与全账户杠杆取小。止损离现价太远、方向不对或数量不足时抛 PlanError"""
    sign = 1 if direction == "long" else -1
    stop_pct = sign * (price - stop) / price
    if stop_pct <= 0:
        raise PlanError([f"止损 {stop} 已越过现价 {price}"])
    if stop_pct > MAX_STOP_PCT:
        raise PlanError([f"止损 {stop} 距现价 {price} {stop_pct * 100:.2f}%，超过 {MAX_STOP_PCT * 100:.0f}%"])
    unit_loss = abs(price - stop) + (price + stop) * TAKER_FEE_RATE
    risk_amount = capital * RISK_PCT * (LOW_CONFIDENCE_FACTOR if confidence == "low" else 1)
    max_notional = capital * MAX_LEVERAGE - other_notional
    if max_notional <= 0:
        raise PlanError([f"交易列表其他币的名义仓位 {other_notional:.2f} 已达到 {MAX_LEVERAGE} 倍交易资金，不能开新仓"])
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
    """校验计划；action=plan 时返回待确认入场和预估（按确认价入场、止损放在区间远端外）。不通过时抛 PlanError"""
    errors = check_action(plan, ctx)
    if plan.action != "plan":
        if errors:
            raise PlanError(errors)
        return {}
    missing = [name for name in ("direction", "confidence", "setup", "valid_hours") if getattr(plan, name) is None]
    if missing:
        raise PlanError([*errors, f"action=plan 时缺少：{', '.join(missing)}"])
    direction, setup, mark = plan.direction, plan.setup, ctx.mark_price
    long = direction == "long"
    if setup.zone_low >= setup.zone_high:
        errors.append(f"区间下沿 {setup.zone_low} 须低于上沿 {setup.zone_high}")
    # 确认 = 进区后收盘回到区间外侧；确认价在区间内会让刚碰到区间边缘、没有真正回落或收回的 K 线也触发
    if long and setup.confirm_price < setup.zone_high:
        errors.append(f"做多的确认价 {setup.confirm_price} 须不低于区间上沿 {setup.zone_high}（进区后收盘回到区间上方才算确认）")
    if not long and setup.confirm_price > setup.zone_low:
        errors.append(f"做空的确认价 {setup.confirm_price} 须不高于区间下沿 {setup.zone_low}（进区后收盘回到区间下方才算确认）")
    if long and mark < setup.zone_low:
        errors.append(f"现价 {mark} 已跌破区间下沿 {setup.zone_low}，支撑已破")
    if not long and mark > setup.zone_high:
        errors.append(f"现价 {mark} 已突破区间上沿 {setup.zone_high}，压力已破")
    if plan.valid_hours > SETUP_VALID_HOURS:
        errors.append(f"valid_hours 不能超过 {SETUP_VALID_HOURS}")
    if errors:
        raise PlanError(errors)
    far_edge = setup.zone_low if long else setup.zone_high
    stop = setup_stop(direction, far_edge, ctx.atr, ctx.rules.tick_size)
    try:
        estimate = size_entry(direction, setup.confirm_price, stop, capital=ctx.capital, other_notional=ctx.other_notional,
                              confidence=plan.confidence, rules=ctx.rules)
    except PlanError as e:
        raise PlanError([f"按确认价入场、止损放在区间{'下沿' if long else '上沿'}外 {STOP_ATR_FACTOR}×ATR(5m)={stop} 估算：{e}。"
                         "区间太宽或离确认价太远，触发后也会被放弃，请收窄区间或调整确认价"]) from e
    return {
        "direction": direction,
        "setup": {k: str(getattr(setup, k)) for k in ("zone_low", "zone_high", "confirm_price")},
        "estimate": estimate,
        "atr": str(ctx.atr.quantize(ctx.rules.tick_size)),
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
    sizing: dict, *, entry_sent: bool, entry_fill: tuple[Decimal, Decimal] | None, tp_filled: list[Decimal],
    position_qty: Decimal, stop_price: Decimal, rules: SymbolRules,
) -> list[OrderSpec]:
    """已触发的计划在当前状态下应挂的单。

    - 入场：还没下过入场单时一张市价单；只下一次，被拒不重试。
    - 止损：有持仓或入场还没成交（未下单、已发出待成交）时一张只减仓止损，数量取计划数量与持仓的较大者（大于持仓时交易所只平实际持仓）。
    - 止盈：有持仓时按入场成交（数量，均价）排出阶梯，减去各档已成交。
    """
    direction = sizing["direction"]
    open_side, close_side = ("BUY", "SELL") if direction == "long" else ("SELL", "BUY")
    quantity = Decimal(sizing["entry"]["quantity"])
    orders = []
    if not entry_sent:
        orders.append(OrderSpec("entry", 0, open_side, "MARKET", None, None, quantity))
    if position_qty > 0 or entry_fill is None:
        orders.append(OrderSpec("stop", None, close_side, "STOP_MARKET", None, stop_price, max(quantity, position_qty)))
    if position_qty > 0 and entry_fill is not None:
        filled, average = entry_fill
        for j, (price, target) in enumerate(take_profit_targets(direction, average, filled, rules)):
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
