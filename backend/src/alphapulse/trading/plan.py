"""交易计划的纯计算：模型提交的计划参数、校验、价格取整、仓位计算、止盈拆分。

下方常量与 main/README 第 1、2 节的数字重复，并写进了分析器的提示词；改 README 时要同步修改。
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Angle = Literal["short", "long"]
Direction = Literal["long", "short"]

# 所有币合计每天（北京时间）最多自动分析的次数
DAILY_ANALYSIS_LIMIT = 60
RISK_PCT = {"short": Decimal("0.01"), "long": Decimal("0.02")}
LOW_CONFIDENCE_FACTOR = Decimal("0.5")
MAX_LEVERAGE = 3
MIN_REWARD_RISK = Decimal("1.5")
ENTRY_VALID_HOURS = {"short": 4, "long": 24}
MAX_HOLD_HOURS = {"short": 24, "long": 48}
# 入场档数（最少，最多）
ENTRY_COUNT = {"short": (1, 1), "long": (1, 3)}
# 止盈档数由角度决定：短线止盈 1 平一半、止盈 2 平剩余；长线 50/30/20
TAKE_PROFIT_RATIOS = {"short": (Decimal("0.5"), Decimal("0.5")), "long": (Decimal("0.5"), Decimal("0.3"), Decimal("0.2"))}
# 短线失效立即市价平仓，要用短周期；长线等 4h 收盘后复核
INVALIDATION_TIMEFRAMES = {"short": ("1m", "15m"), "long": ("4h",)}
MAX_REEVALUATE = 4
# 开仓、止损都按吃单手续费估算（C1 实测 taker 0.04%）
TAKER_FEE_RATE = Decimal("0.0004")
# 模拟盘 PERCENT_PRICE：买单价不高于标记价 × 1.05，卖单价不低于标记价 × 0.95
PERCENT_PRICE_LIMIT = Decimal("0.05")
RATIO_TOLERANCE = Decimal("0.001")

Price = Annotated[Decimal, Field(gt=0)]


class Condition(BaseModel):
    direction: Annotated[Literal["above", "below"], Field(description="above：价格高于 price 时满足；below：低于时满足")]
    price: Price
    timeframe: Annotated[
        Literal["mark", "1m", "15m", "1h", "4h"],
        Field(description="mark：标记价触及即满足；其他：该周期 K 线收盘价满足才算"),
    ]
    basis: Annotated[str, Field(description="依据，如对应的支撑区、压力区")]


class Entry(BaseModel):
    price: Price
    ratio: Annotated[Decimal, Field(gt=0, le=1, description="占总仓位的比例，所有档位之和为 1")]
    basis: str


class TakeProfit(BaseModel):
    price: Price
    basis: str


class TradePlan(BaseModel):
    """submit_trade_plan 的参数"""

    action: Annotated[Literal["plan", "keep", "exit", "wait"], Field(description=(
        "plan：给出新计划（替换当前计划）；keep：当前计划不变；"
        "exit：结束当前计划，撤销未成交的入场单，有持仓则市价平仓；wait：观望，不给计划（仅在没有持仓和当前计划时）"
    ))]
    summary: Annotated[str, Field(description="一句话结论，含方向和置信度")]
    reasons: Annotated[str, Field(description="依据要点：新闻、结构、衍生品，引用新闻写 [新闻 id]")]
    risks: Annotated[str, Field(description="持仓时限内的重大事件及时间、与结论相反的信号")]
    reevaluate: Annotated[list[Condition], Field(max_length=MAX_REEVALUATE, description=(
        "满足任一条件时程序重新请你分析：用来表达另一侧方向、突破回踩等条件计划、需要复核的位置。提交时不能已经满足"
    ))] = []
    direction: Direction | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    entries: Annotated[list[Entry] | None, Field(description="入场限价单，每档一张")] = None
    stop: Annotated[Price | None, Field(description="统一止损价")] = None
    stop_basis: str | None = None
    take_profits: Annotated[list[TakeProfit] | None, Field(description="按价格由近到远排列")] = None
    entry_valid_hours: Annotated[Decimal | None, Field(gt=0, description="入场单有效小时数，到期未成交的档位撤销")] = None
    max_hold_hours: Annotated[Decimal | None, Field(gt=0, description="首档成交起最长持仓小时数，到时市价平仓")] = None
    invalidation: Annotated[Condition | None, Field(description="失效条件：做多为 below，做空为 above")] = None


@dataclass
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal


@dataclass
class PlanContext:
    angle: Angle
    mark_price: Decimal
    # min(设定的交易资金, 账户权益)
    capital: Decimal
    # 交易列表中其他币的持仓名义价值合计
    other_notional: Decimal
    rules: SymbolRules
    has_plan: bool
    position_side: Direction | None


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


def reward_risk(entry: Decimal, stop: Decimal, targets: list[Decimal], ratios: tuple[Decimal, ...]) -> Decimal:
    """综合盈亏比 = Σ(止盈比例 × |止盈价 − 入场价|) ÷ |入场价 − 止损价|，与 README 第 4 节一致（不含手续费）"""
    reward = sum((r * abs(t - entry) for t, r in zip(targets, ratios, strict=True)), Decimal(0))
    return reward / abs(entry - stop)


def check_action(plan: TradePlan, ctx: PlanContext) -> list[str]:
    errors = []
    if plan.action == "keep" and not ctx.has_plan:
        errors.append("当前没有计划，不能用 keep")
    if plan.action == "exit" and not ctx.has_plan and ctx.position_side is None:
        errors.append("当前没有计划也没有持仓，不能用 exit，观望请用 wait")
    if plan.action == "wait" and (ctx.has_plan or ctx.position_side is not None):
        errors.append("有当前计划或持仓时不能用 wait：保留用 keep，结束用 exit")
    for i, c in enumerate(plan.reevaluate, 1):
        satisfied = ctx.mark_price > c.price if c.direction == "above" else ctx.mark_price < c.price
        if satisfied:
            errors.append(f"reevaluate 第 {i} 个条件（{c.direction} {c.price}）按现价 {ctx.mark_price} 已经满足，提交后会立即触发")
    return errors


def check_plan_fields(plan: TradePlan, ctx: PlanContext) -> list[str]:
    """action=plan 时的字段完整性、档数、比例、有效期"""
    required = ("direction", "confidence", "entries", "stop", "take_profits", "entry_valid_hours", "max_hold_hours", "invalidation")
    missing = [name for name in required if getattr(plan, name) is None]
    if missing:
        return [f"action=plan 时缺少：{', '.join(missing)}"]
    errors = []
    angle = ctx.angle
    label = "短线" if angle == "short" else "长线"
    low, high = ENTRY_COUNT[angle]
    if not low <= len(plan.entries) <= high:
        errors.append(f"{label}入场档数须为 {low}–{high}，现为 {len(plan.entries)}" if low != high else f"{label}只能 {low} 档入场，现为 {len(plan.entries)}")
    if abs(sum((e.ratio for e in plan.entries), Decimal(0)) - 1) > RATIO_TOLERANCE:
        errors.append("入场各档比例之和须为 1")
    if len(plan.take_profits) != len(TAKE_PROFIT_RATIOS[angle]):
        errors.append(f"{label}须给 {len(TAKE_PROFIT_RATIOS[angle])} 个止盈，现为 {len(plan.take_profits)}")
    if plan.entry_valid_hours > ENTRY_VALID_HOURS[angle]:
        errors.append(f"{label} entry_valid_hours 不能超过 {ENTRY_VALID_HOURS[angle]}")
    if plan.max_hold_hours > MAX_HOLD_HOURS[angle]:
        errors.append(f"{label} max_hold_hours 不能超过 {MAX_HOLD_HOURS[angle]}")
    if plan.invalidation.timeframe not in INVALIDATION_TIMEFRAMES[angle]:
        errors.append(f"{label}失效条件的周期只能是 {'/'.join(INVALIDATION_TIMEFRAMES[angle])}")
    if ctx.position_side is not None and plan.direction != ctx.position_side:
        errors.append(f"已有{'多' if ctx.position_side == 'long' else '空'}头持仓，新计划方向必须一致；要反手请先用 exit")
    return errors


def check_prices(direction: Direction, entries: list[Decimal], stop: Decimal, targets: list[Decimal], plan: TradePlan, ctx: PlanContext) -> list[str]:
    """价格顺序：做多 止损 < 失效价 < 入场 < 止盈 1 < 止盈 2 < …，止损低于现价、止盈 1 高于现价；做空镜像"""
    errors = []
    sign = 1 if direction == "long" else -1
    mark = ctx.mark_price
    invalidation = plan.invalidation.price

    def below(a: Decimal, b: Decimal) -> bool:
        # 做多时 a < b；做空时 a > b
        return sign * (b - a) > 0

    side = "做多" if direction == "long" else "做空"
    if plan.invalidation.direction != ("below" if direction == "long" else "above"):
        errors.append(f"{side}的失效条件方向应为 {'below' if direction == 'long' else 'above'}")
    if not below(stop, invalidation):
        errors.append(f"{side}时止损 {stop} 应在失效价 {invalidation} 之外")
    nearest_entry = max(entries) if direction == "long" else min(entries)
    farthest_entry = min(entries) if direction == "long" else max(entries)
    if not below(invalidation, farthest_entry):
        errors.append(f"{side}时失效价 {invalidation} 应在所有入场价之外")
    if not below(stop, mark):
        errors.append(f"{side}时止损 {stop} 应在现价 {mark} 之外，否则挂单会立即触发")
    if len(set(entries)) != len(entries):
        errors.append("入场档位价格重复")
    if targets and not below(nearest_entry, targets[0]):
        errors.append(f"{side}时止盈 1 {targets[0]} 应在所有入场价之外的盈利方向")
    if targets and not below(mark, targets[0]):
        errors.append(f"止盈 1 {targets[0]} 已越过现价 {mark}，挂单会立即成交")
    if any(not below(a, b) for a, b in zip(targets, targets[1:], strict=False)):
        errors.append("止盈价须按由近到远排列且不重复")
    limit = mark * (1 + PERCENT_PRICE_LIMIT) if direction == "long" else mark * (1 - PERCENT_PRICE_LIMIT)
    if any(below(limit, e) for e in entries):
        errors.append(f"入场价超出交易所限价范围（{'不高于' if direction == 'long' else '不低于'} {limit:.2f}）")
    return errors


def size_plan(plan: TradePlan, ctx: PlanContext) -> dict:
    """校验计划并计算仓位；不通过时抛 PlanError。返回值可直接存 JSONB（数值为字符串）"""
    errors = check_action(plan, ctx)
    if plan.action != "plan":
        if errors:
            raise PlanError(errors)
        return {}
    errors += check_plan_fields(plan, ctx)
    if errors:
        raise PlanError(errors)

    angle, direction, rules = ctx.angle, plan.direction, ctx.rules
    # 向保守方向取整：做多的入场、止损、止盈都向下（入场更低、止损更远、止盈更近），做空都向上
    rounding = ROUND_FLOOR if direction == "long" else ROUND_CEILING
    entry_prices = [round_to_step(e.price, rules.tick_size, rounding) for e in plan.entries]
    stop = round_to_step(plan.stop, rules.tick_size, rounding)
    targets = [round_to_step(t.price, rules.tick_size, rounding) for t in plan.take_profits]
    ratios = [e.ratio / sum((e.ratio for e in plan.entries), Decimal(0)) for e in plan.entries]
    tp_ratios = TAKE_PROFIT_RATIOS[angle]

    errors = check_prices(direction, entry_prices, stop, targets, plan, ctx)
    if errors:
        raise PlanError(errors)

    average = sum((r * p for r, p in zip(ratios, entry_prices, strict=True)), Decimal(0))
    rr = reward_risk(average, stop, targets, tp_ratios) if angle == "long" else reward_risk(average, stop, targets[:1], (Decimal(1),))
    if rr < MIN_REWARD_RISK:
        basis = "50/30/20 加权" if angle == "long" else "止盈 1"
        raise PlanError([f"盈亏比 {rr:.2f}（按{basis}）低于 {MIN_REWARD_RISK}。止盈必须放在有依据的区，不得为凑盈亏比倒推；凑不够就不给这个方向"])
    nearest_entry = max(entry_prices) if direction == "long" else min(entry_prices)
    rr_first = reward_risk(nearest_entry, stop, targets, tp_ratios) if angle == "long" else rr

    def unit_loss(price: Decimal) -> Decimal:
        return abs(price - stop) + (price + stop) * TAKER_FEE_RATE

    risk_amount = ctx.capital * RISK_PCT[angle] * (LOW_CONFIDENCE_FACTOR if plan.confidence == "low" else 1)
    max_notional = ctx.capital * MAX_LEVERAGE - ctx.other_notional
    if max_notional <= 0:
        raise PlanError([f"交易列表其他币的名义仓位 {ctx.other_notional:.2f} 已达到 {MAX_LEVERAGE} 倍交易资金，不能开新仓"])
    by_risk = risk_amount / unit_loss(average)
    by_leverage = max_notional / average
    total = min(by_risk, by_leverage)
    quantities = [round_to_step(total * r, rules.step_size, ROUND_FLOOR) for r in ratios]
    small = [
        f"第 {i} 档 {p} × {q} = {p * q:.2f} USDT"
        for i, (p, q) in enumerate(zip(entry_prices, quantities, strict=True), 1) if p * q < rules.min_notional
    ]
    if small:
        raise PlanError([
            f"入场档位名义价值低于最小下单金额 {rules.min_notional} USDT：{'、'.join(small)}。"
            f"本次可用风险金额 {risk_amount:.2f} USDT、总数量约 {total:.6f}，请合并或删档"
        ])

    quantity = sum(quantities, Decimal(0))
    notional = sum((p * q for p, q in zip(entry_prices, quantities, strict=True)), Decimal(0))
    risk_at_stop = sum((q * unit_loss(p) for p, q in zip(entry_prices, quantities, strict=True)), Decimal(0))
    tp_quantities = split_take_profits(quantity, tp_ratios, rules.step_size)
    return {
        "direction": direction,
        "entries": [
            {"price": str(p), "ratio": str(r.quantize(Decimal("0.0001"))), "quantity": str(q), "notional": str((p * q).quantize(Decimal("0.01")))}
            for p, r, q in zip(entry_prices, ratios, quantities, strict=True)
        ],
        "stop": str(stop),
        "take_profits": [
            {"price": str(t), "ratio": str(r), "quantity": str(q)} for t, r, q in zip(targets, tp_ratios, tp_quantities, strict=True)
        ],
        "average_price": str(average.quantize(rules.tick_size)),
        "quantity": str(quantity),
        "notional": str(notional.quantize(Decimal("0.01"))),
        "capital": str(ctx.capital),
        "leverage": str((notional / ctx.capital).quantize(Decimal("0.01"))),
        "risk_amount": str(risk_amount.quantize(Decimal("0.01"))),
        "risk_at_stop": str(risk_at_stop.quantize(Decimal("0.01"))),
        "risk_pct": str((risk_at_stop / ctx.capital * 100).quantize(Decimal("0.01"))),
        "capped_by_leverage": by_leverage < by_risk,
        "reward_risk": str(rr.quantize(Decimal("0.01"))),
        "reward_risk_first_only": str(rr_first.quantize(Decimal("0.01"))),
    }
