"""模拟盘分析器：为一个币的一个角度调用模型，产出经程序校验、可自动执行的交易计划。

只开放只读工具和 submit_trade_plan；行情预先取好放进用户消息，减少工具往返。
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select

from alphapulse.ai import llm
from alphapulse.ai.prompt import data_overview, estimate_tokens, skills_section
from alphapulse.ai.tools import TOOLS, dump, local, run_tool
from alphapulse.core.db import SessionLocal
from alphapulse.models import AiProvider, TradingAccount, TradingAnalysis, TradingEvent, TradingOrder, TradingSymbol
from alphapulse.trading import plan as rules
from alphapulse.trading.client import OWN_CLIENT_ID, ExchangeError, mark_price, symbol_rules
from alphapulse.trading.plan import PlanContext, PlanError, TradePlan, size_plan
from alphapulse.trading.snapshot import read_snapshot

logger = logging.getLogger("alphapulse.trading.analyst")

READ_TOOLS = ("search_news", "get_news", "search_symbols", "get_klines", "get_ticker", "get_derivatives")
# 最后一轮只开放 submit_trade_plan，逼模型给出结论
MAX_ROUNDS = 6
RECENT_DECISIONS = 2
ANGLE_LABELS = {"short": "短线（24 小时内平仓）", "long": "长线（12–48 小时内平仓）"}
DIRECTION_LABELS = {"long": "做多", "short": "做空"}
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
ACTION_LABELS = {"plan": "新计划", "keep": "维持当前计划", "exit": "结束当前计划", "wait": "观望"}
# 与 README 第 1 节一致：定方向和找区的周期、衍生品参数
KLINE_INTERVALS = {"short": ("4h", "1h", "15m"), "long": ("1d", "4h", "1h")}
DERIVATIVES_PARAMS = {"short": ("1h", 24), "long": ("4h", 18)}


def _pct(value: Decimal) -> str:
    return f"{(value * 100).normalize()}%"


INSTRUCTIONS = f"""你是 AlphaPulse 的模拟盘交易分析器：为一个币种的一个角度给出由程序自动执行的交易计划。

## 任务
- 只分析用户消息指定的币种和角度（短线或长线），不给另一个角度。
- 下方 Skills 是交易规范。以下几处按本节执行，与 Skills 冲突时以本节为准：
  - main/README 第 1 节「每次同时给出两个角度」不适用，只给指定角度。
  - main/README 第 8 节的输出表格不适用：用简短中文写出结论、用到的支撑区与压力区、依据（新闻、结构、衍生品）和风险；计划的数字只通过 submit_trade_plan 提交。
  - main/README 第 9 节不适用：没有 CONTEXT.md，不要写。之前的结论见用户消息里的「最近决策」，按 README 第 6 节做实时复核，不直接沿用旧点位。
  - main/README 5.4 的条件计划（突破回踩、事件落地后再入场等）不能直接挂单，改写成 reevaluate 条件：价格到达后程序会重新请你分析。
  - 数量由程序按交易资金计算，你只给价格、档位比例和置信度。
- 用户消息里已预先取好实盘行情（ticker、K 线、衍生品），不要用相同参数重复调用；需要其他周期或其他币时再调用。
- 新闻没有预取，必须按 main/README 第 3 节第 3 步用 search_news 检索相关新闻，并查清持仓时限内的重大数据及公布时间。
- 最后必须调用一次 submit_trade_plan。校验不通过会返回原因，按原因修正后重新提交。

## 程序如何执行计划
- 入场：每档一张限价单；到 entry_valid_hours 仍未成交的档位撤销。买单价高于现价（卖单价低于现价）会立即成交。
- 止损：与入场单同时在交易所挂只减仓止损单（按标记价触发），首档成交即受保护。
- 止盈：短线止盈 1 平一半、止盈 2 平剩余；长线 3 个止盈依次平 50% / 30% / 20%。止盈 1 成交后撤销剩余入场单，止损移到保本价。
- invalidation（失效条件）：短线用 1m 或 15m 收盘，触发后程序立即市价平仓；长线只能用 4h 收盘，触发后程序请你复核。
- reevaluate：任一条件满足时程序重新请你分析（最多 {rules.MAX_REEVALUATE} 个）。用来表达另一侧方向、条件计划和需要复核的位置。
- max_hold_hours 从首档成交起算，到时程序市价平仓。

## 程序的硬性校验
- 短线 1 档入场、2 个止盈；长线 1–3 档入场、3 个止盈；入场比例之和为 1。
- 做多：止损 < 失效价 < 所有入场价 < 止盈 1 < 止盈 2 < 止盈 3，且止损低于现价、止盈 1 高于现价；做空反过来。
- 盈亏比 ≥ {rules.MIN_REWARD_RISK}：短线按止盈 1，长线按 50/30/20 加权（全部档位成交时）。
- entry_valid_hours：短线 ≤ {rules.ENTRY_VALID_HOURS['short']}，长线 ≤ {rules.ENTRY_VALID_HOURS['long']}；max_hold_hours：短线 ≤ {rules.MAX_HOLD_HOURS['short']}，长线 ≤ {rules.MAX_HOLD_HOURS['long']}。
- 每档入场的名义价值不低于交易所最小下单金额，不足时合并或删档。
- 单笔风险为交易资金的 {_pct(rules.RISK_PCT['short'])}（短线）或 {_pct(rules.RISK_PCT['long'])}（长线），置信度低时减半；名义仓位合计不超过 {rules.MAX_LEVERAGE} 倍交易资金。这两条由程序计算数量来保证。
- 有持仓时新计划的方向必须与持仓一致；要反手先用 exit。

## 有持仓时
- action=plan 只用来调整止损（只能收紧）、止盈、失效条件和 reevaluate：entries 留空，不加仓（README 第 10 节禁止计划外加仓）。止盈按当前持仓数量拆分；平仓截止时间沿用首档成交时确定的，不延长。
- 计划仍然合适就用 keep；要提前结束用 exit（撤销未成交的入场单并市价平仓）。
- 计划提交后由执行器在下一轮（约 2 秒内）按最新持仓核对后生效；期间持仓变了，计划作废并重新请你分析。
"""

SUBMIT_SPEC = llm.ToolSpec(
    "submit_trade_plan",
    "提交本次分析的结论和计划。程序会校验并计算仓位；不通过时返回原因，修正后重新提交。",
    TradePlan.model_json_schema(),
)


class AnalysisError(Exception):
    """分析无法完成；message 写入分析记录和时间线"""


@dataclass
class Prepared:
    provider: AiProvider
    effort: str | None
    dry_run: bool
    angle: str
    system: str
    user_text: str
    context: PlanContext
    # 当前计划的仓位计算结果，提交时重算当前止损用
    current_sizing: dict | None


def plan_brief(decision: dict, sizing: dict | None) -> dict:
    """给模型看的计划摘要：只留价格和状态，不带长段文字"""
    brief = {k: decision.get(k) for k in ("action", "summary", "direction", "confidence", "stop", "entry_valid_hours", "max_hold_hours")}
    brief["entries"] = [{"price": e["price"], "ratio": e["ratio"]} for e in decision.get("entries") or []]
    brief["take_profits"] = [t["price"] for t in decision.get("take_profits") or []]
    brief["invalidation"] = decision.get("invalidation")
    brief["reevaluate"] = [{k: c[k] for k in ("direction", "price", "timeframe")} for c in decision.get("reevaluate") or []]
    if sizing:
        brief["sizing"] = {k: sizing.get(k) for k in ("quantity", "notional", "risk_at_stop", "reward_risk")}
    return brief


async def prefetch(symbol: str, angle: str) -> str:
    """预取行情：工具出错时把错误原样放进消息，不中断分析"""
    period, limit = DERIVATIVES_PARAMS[angle]
    calls = [("get_ticker", {"symbol": symbol})]
    calls += [("get_klines", {"symbol": symbol, "interval": i, "limit": 200}) for i in KLINE_INTERVALS[angle]]
    calls.append(("get_derivatives", {"symbol": symbol, "period": period, "limit": limit}))
    outcomes = await asyncio.gather(*(run_tool(name, args) for name, args in calls))
    return "\n\n".join(f"### {name} {json.dumps(args, ensure_ascii=False)}\n{o.result}" for (name, args), o in zip(calls, outcomes, strict=True))


async def prepare(symbol: str, reasons: list[str], analysis_id: int) -> Prepared:
    async with SessionLocal() as session:
        account = await session.get(TradingAccount, 1)
        target = await session.get(TradingSymbol, symbol)
        if target is None:
            raise AnalysisError(f"{symbol} 已不在交易列表中")
        if account is None or account.capital_usdt is None:
            raise AnalysisError("还没有设置交易资金")
        provider = await session.get(AiProvider, account.provider_id) if account.provider_id else await session.scalar(
            select(AiProvider).where(AiProvider.is_default)
        )
        if provider is None:
            raise AnalysisError("没有可用的模型配置，请先到「环境变量」页面添加")
        session.expunge(provider)
        symbols = list((await session.scalars(select(TradingSymbol.symbol))).all())
        recent = (await session.scalars(
            select(TradingAnalysis)
            .where(TradingAnalysis.symbol == symbol, TradingAnalysis.status == "done", TradingAnalysis.id != analysis_id)
            .order_by(TradingAnalysis.id.desc()).limit(RECENT_DECISIONS)
        )).all()
        current = await session.scalar(
            select(TradingAnalysis)
            .where(TradingAnalysis.symbol == symbol, TradingAnalysis.plan_status.in_(("pending", "open", "closing")), TradingAnalysis.dry_run.is_(False))
            .order_by(TradingAnalysis.id.desc()).limit(1)
        )
        fills = [
            {"purpose": o.purpose, "leg": o.leg, "filled": str(o.filled_qty), "avg_price": str(o.avg_price), "status": o.status}
            for o in (await session.scalars(
                select(TradingOrder).where(TradingOrder.analysis_id == current.id, TradingOrder.filled_qty > 0).order_by(TradingOrder.id)
            )).all()
        ] if current else []
        system = "\n\n".join([INSTRUCTIONS, "## 数据概况", await data_overview(session), await skills_section(session)])
        angle, dry_run = target.angle, not account.running
        effort = account.reasoning_effort or provider.reasoning_effort

    snapshot = await read_snapshot()
    if not snapshot or not snapshot["account"]:
        raise AnalysisError("取不到模拟盘账户快照（引擎未同步到币安），无法计算仓位")
    equity = Decimal(snapshot["account"]["margin_balance"])
    capital = min(account.capital_usdt, equity)
    position = next((p for p in snapshot["positions"] if p["symbol"] == symbol), None)
    other_notional = sum((Decimal(p["notional"]) for p in snapshot["positions"] if p["symbol"] != symbol and p["symbol"] in symbols), Decimal(0))
    symbol_rules_ = await symbol_rules(symbol)
    mark = await mark_price(symbol)
    context = PlanContext(
        angle=angle, mark_price=mark, capital=capital, other_notional=other_notional, rules=symbol_rules_, has_plan=current is not None,
        position_side=None,
    )
    apply_position(context, snapshot, symbol, current.sizing if current else None)

    orders = [o for o in snapshot["orders"] + snapshot["algo_orders"] if o["symbol"] == symbol]
    risk = capital * rules.RISK_PCT[angle]
    lines = [
        "## 任务",
        f"- 币种：{symbol}（币安 U 本位永续）",
        f"- 角度：{ANGLE_LABELS[angle]}",
        f"- 触发原因：{'；'.join(reasons)}",
        f"- 运行模式：{'试运行（引擎已停止，计划只记录、不下单）' if dry_run else '交易中（计划会在模拟盘执行）'}",
        "",
        "## 账户（程序据此计算数量）",
        f"- 交易资金：{capital:.2f} USDT（设定 {account.capital_usdt} USDT，账户权益 {equity:.2f} USDT，取较小者）",
        f"- 本次单笔风险金额：{risk:.2f} USDT（{_pct(rules.RISK_PCT[angle])}；置信度低时 {risk * rules.LOW_CONFIDENCE_FACTOR:.2f}）",
        f"- 可用名义仓位：{capital * rules.MAX_LEVERAGE - other_notional:.2f} USDT（{rules.MAX_LEVERAGE} 倍交易资金减去交易列表其他币的持仓 {other_notional:.2f}）",
        f"- 模拟盘交易规则：价格精度 {symbol_rules_.tick_size}，数量精度 {symbol_rules_.step_size}，最小下单金额 {symbol_rules_.min_notional} USDT",
        f"- 模拟盘标记价：{mark}（校验以此为准；行情数据来自实盘，两者通常相差不到 0.1%）",
        "",
        "## 当前持仓与挂单（模拟盘）",
        f"- 持仓：{dump(position) if position else '无'}",
        f"- 挂单：{dump(orders) if orders else '无'}",
        "",
        "## 当前计划",
        dump({
            "id": current.id, "plan_status": current.plan_status,
            "valid_until": current.valid_until and local(current.valid_until),
            "first_fill_at": current.first_fill_at and local(current.first_fill_at),
            "close_deadline": current.close_deadline and local(current.close_deadline),
            "current_stop": str(context.current_stop) if context.current_stop else None,
            "fills": fills,
            **plan_brief(current.decision, current.sizing),
        }) if current else "无",
        "",
        "## 最近决策（新的在前）",
        *([f"- {local(a.started_at)}{'（试运行）' if a.dry_run else ''}：{dump(plan_brief(a.decision, a.sizing))}" for a in recent] or ["无"]),
        "",
        "## 预取的行情（实盘数据，已取，不要用相同参数重复调用）",
        await prefetch(symbol, angle),
    ]
    return Prepared(provider=provider, effort=effort, dry_run=dry_run, angle=angle, system=system, user_text="\n".join(lines),
                    context=context, current_sizing=current.sizing if current else None)


def apply_position(context: PlanContext, snapshot: dict, symbol: str, current_sizing: dict | None) -> None:
    """按快照填入持仓与当前止损。当前止损取交易所上本程序止损单里最紧的一张（可能已移到保本价），没有则取当前计划的止损"""
    position = next((p for p in snapshot["positions"] if p["symbol"] == symbol), None)
    if position is None:
        context.position_side, context.position_qty, context.position_entry, context.current_stop = None, Decimal(0), None, None
        return
    side = "long" if position["side"] == "LONG" else "short"
    close_side = "SELL" if side == "long" else "BUY"
    stops = [
        Decimal(o["trigger_price"]) for o in snapshot["algo_orders"]
        if o["symbol"] == symbol and o["side"] == close_side and o["type"].startswith("STOP") and OWN_CLIENT_ID.match(o["client_id"])
    ]
    if stops:
        current_stop = max(stops) if side == "long" else min(stops)
    else:
        current_stop = Decimal(current_sizing["stop"]) if current_sizing else None
    context.position_side, context.position_qty = side, Decimal(position["amount"])
    context.position_entry, context.current_stop = Decimal(position["entry_price"]), current_stop


async def complete(prepared: Prepared, messages: list[dict], tools: list[llm.ToolSpec]) -> llm.Completion:
    async for item in llm.stream(prepared.provider, prepared.system, messages, tools, prepared.effort):
        if isinstance(item, llm.Completion):
            return item
    raise AssertionError("llm.stream 没有产出 Completion")


async def add_event(symbol: str, analysis_id: int, kind: str, message: str, level: str = "info", data: dict | None = None) -> None:
    async with SessionLocal() as session:
        session.add(TradingEvent(symbol=symbol, analysis_id=analysis_id, kind=kind, level=level, message=message, data=data))
        await session.commit()


def describe(plan: TradePlan, sizing: dict, dry_run: bool) -> str:
    prefix = "试运行" if dry_run else ""
    if plan.action != "plan":
        return f"{prefix}{ACTION_LABELS[plan.action]}：{plan.summary}"
    entries = "、".join(f"{e['price']}×{e['quantity']}" for e in sizing["entries"])
    targets = " / ".join(t["price"] for t in sizing["take_profits"])
    return (
        f"{prefix}计划：{DIRECTION_LABELS[plan.direction]}，置信度{CONFIDENCE_LABELS[plan.confidence]}；入场 {entries}，"
        f"止损 {sizing['stop']}，止盈 {targets}；止损亏损 {sizing['risk_at_stop']} USDT（交易资金的 {sizing['risk_pct']}%），"
        f"盈亏比 {sizing['reward_risk']}（仅首档 {sizing['reward_risk_first_only']}）。{plan.summary}"
    )


async def submit(prepared: Prepared, symbol: str, analysis_id: int, arguments: dict) -> tuple[str, tuple[TradePlan, dict] | None]:
    try:
        plan = TradePlan.model_validate(arguments)
    except ValidationError as e:
        return dump({"error": "参数无效", "details": e.errors(include_url=False)}), None
    # 分析可能持续几分钟，按提交时的标记价和持仓校验
    prepared.context.mark_price = await mark_price(symbol)
    snapshot = await read_snapshot()
    if snapshot:
        apply_position(prepared.context, snapshot, symbol, prepared.current_sizing)
    try:
        sizing = size_plan(plan, prepared.context)
    except PlanError as e:
        await add_event(symbol, analysis_id, "plan_rejected", f"计划未通过校验，已退回模型修正：{'；'.join(e.errors)}", "warn")
        return dump({"error": "计划未通过校验", "details": e.errors, "mark_price": str(prepared.context.mark_price)}), None
    return dump({"status": "已接受", "sizing": sizing}), (plan, sizing)


async def run_analysis(symbol: str, reasons: list[str]) -> None:
    """一次完整的分析；结果和失败原因都写进 trading_analyses 和时间线，不向外抛出"""
    async with SessionLocal() as session:
        account = await session.get(TradingAccount, 1)
        target = await session.get(TradingSymbol, symbol)
        analysis = TradingAnalysis(
            symbol=symbol, angle=target.angle if target else "long", reasons=reasons,
            dry_run=not (account and account.running), status="running",
        )
        session.add(analysis)
        await session.commit()
        analysis_id, dry_run = analysis.id, analysis.dry_run
    await add_event(symbol, analysis_id, "analysis_started", f"开始{'试运行' if dry_run else ''}分析：{'；'.join(reasons)}")

    messages: list[dict] = []
    texts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    accepted: tuple[TradePlan, dict] | None = None
    prepared: Prepared | None = None
    try:
        prepared = await prepare(symbol, reasons, analysis_id)
        estimated = estimate_tokens(prepared.system) + estimate_tokens(prepared.user_text)
        if estimated + prepared.provider.max_tokens > prepared.provider.context_window:
            raise AnalysisError(f"提示词约 {estimated} token，加上最大输出超过模型上下文上限 {prepared.provider.context_window}")
        messages.append({"role": "user", "text": prepared.user_text})
        specs = [llm.ToolSpec(name, TOOLS[name].description, TOOLS[name].args_model.model_json_schema()) for name in READ_TOOLS]
        for round_no in range(MAX_ROUNDS):
            last = round_no == MAX_ROUNDS - 1
            completion = await complete(prepared, messages, [SUBMIT_SPEC] if last else [*specs, SUBMIT_SPEC])
            for key in usage:
                usage[key] += completion.usage.get(key, 0)
            if completion.text.strip():
                texts.append(completion.text.strip())
            assistant = {"role": "assistant", "text": completion.text, "tool_calls": completion.tool_calls}
            if completion.thinking_blocks:
                assistant["thinking_blocks"] = completion.thinking_blocks
            messages.append(assistant)
            if not completion.tool_calls:
                if completion.truncated:
                    raise AnalysisError(f"模型输出达到最大输出上限（{prepared.provider.max_tokens} token）被截断")
                messages.append({"role": "user", "text": "请调用 submit_trade_plan 提交结论。"})
                continue
            for call in completion.tool_calls:
                if completion.truncated:
                    result = llm.TRUNCATED_RESULT
                elif call["name"] == "submit_trade_plan":
                    result, outcome = await submit(prepared, symbol, analysis_id, call["arguments"])
                    accepted = accepted or outcome
                elif call["name"] in READ_TOOLS:
                    result = (await run_tool(call["name"], call["arguments"])).result
                else:
                    result = dump({"error": f"分析时不能使用工具 {call['name']}"})
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "result": result})
            if accepted:
                break
        if accepted is None:
            raise AnalysisError(f"模型在 {MAX_ROUNDS} 轮内没有提交通过校验的结论")
    except (AnalysisError, ExchangeError, llm.LlmError, PlanError) as e:
        error = str(e)
    except Exception as e:
        logger.exception("%s 分析出错", symbol)
        error = f"程序出错：{e!r}"
    else:
        error = None

    async with SessionLocal() as session:
        analysis = await session.get(TradingAnalysis, analysis_id)
        analysis.status = "failed" if error else "done"
        analysis.error = error
        analysis.text = "\n\n".join(texts) or None
        # thinking 块只在同一轮工具循环内需要回传，存档时去掉，减小体积
        analysis.transcript = [{k: v for k, v in m.items() if k != "thinking_blocks"} for m in messages] or None
        analysis.usage = usage
        analysis.finished_at = datetime.now(UTC)
        if prepared:
            analysis.provider_id, analysis.model, analysis.reasoning_effort = prepared.provider.id, prepared.provider.model, prepared.effort
        if accepted:
            plan, sizing = accepted
            analysis.action = plan.action
            analysis.decision = plan.model_dump(mode="json")
            analysis.sizing = sizing or None
            # 交易中的新计划和结束决定交给执行器按最新持仓核对后采纳
            if not dry_run and plan.action in ("plan", "exit"):
                analysis.plan_status = "proposed"
        await session.commit()
    if error:
        await add_event(symbol, analysis_id, "analysis_failed", f"分析失败：{error}", "error")
    else:
        plan, sizing = accepted
        await add_event(symbol, analysis_id, "analysis_done", describe(plan, sizing, dry_run), data={"action": plan.action})
