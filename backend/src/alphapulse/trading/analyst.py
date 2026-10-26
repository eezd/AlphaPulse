"""模拟盘分析器：为一个币调用模型，划出经程序校验、由执行器立即挂限价单执行的短线计划。

只开放只读工具和 submit_trade_plan；行情预先取好放进用户消息，减少工具往返。
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select

from alphapulse.ai import llm
from alphapulse.ai.prompt import data_overview, estimate_tokens, skills_section
from alphapulse.ai.tools import TOOLS, dump, local, run_tool
from alphapulse.api.market import client as market_client
from alphapulse.core.db import SessionLocal
from alphapulse.models import AiProvider, TradingAccount, TradingAnalysis, TradingEvent, TradingOrder, TradingSymbol
from alphapulse.trading import plan as rules
from alphapulse.trading.client import ExchangeError, active_exchange, mark_price, symbol_rules
from alphapulse.trading.executor import CLOSE_REASON_LABELS
from alphapulse.trading.plan import PlanContext, PlanError, TradePlan, atr, check_setup, parse_bars
from alphapulse.trading.schedule import daily_limit_reached
from alphapulse.trading.snapshot import SnapshotError, read_snapshot, require_fresh_snapshot

logger = logging.getLogger("alphapulse.trading.analyst")

READ_TOOLS = ("search_news", "get_news", "search_symbols", "get_klines", "get_ticker", "get_derivatives")
# 最后一轮只开放 submit_trade_plan，逼模型给出结论
MAX_ROUNDS = 6
RECENT_DECISIONS = 2
# skill 的连续止损暂停、止损后冷却、每日开仓上限要靠这段成交记录判断
RECENT_TRADES_HOURS = 24
DIRECTION_LABELS = {"long": "做多", "short": "做空"}
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
ACTION_LABELS = {"plan": "新计划", "keep": "维持当前计划", "exit": "结束当前计划", "wait": "观望"}
# 找区、确认的周期和衍生品参数（与 README 一致）
KLINE_INTERVALS = ("1d", "4h", "1h", "15m", "5m")
DERIVATIVES_PARAMS = ("1h", 24)


def _pct(value: Decimal) -> str:
    return f"{(value * 100).normalize()}%"


def _ladder(direction: str) -> str:
    sign = "+" if direction == "long" else "−"
    steps = rules.TAKE_PROFIT_STEPS[direction]
    return f"{' / '.join(sign + _pct(p) for p, _ in steps)} 分别平 {' / '.join(_pct(r) for _, r in steps)}"


INSTRUCTIONS = f"""你是 AlphaPulse 的模拟盘交易分析器：为一个币种划出短线计划（区间 + 区内的挂单价），由程序立即挂限价单并管理持仓。

## 任务
- 下方 Skills 是交易规范。以下几处按本节执行，与 Skills 冲突时以本节为准：
  - main/README 第 8 节的输出表格不适用：用简短中文写出结论、所用的区间、依据（新闻、结构、衍生品）和风险；数字只通过 submit_trade_plan 提交。
  - main/README 第 9 节不适用：没有 CONTEXT.md，不要写。之前的结论见用户消息里的「最近决策」，要按实时数据复核，不直接沿用旧点位。
  - 数量、止损、止盈由程序计算，你只给方向、区间、挂单价、有效期和置信度。
- 用户消息里已预先取好实盘行情（ticker、{' / '.join(KLINE_INTERVALS)} K 线、衍生品），不要用相同参数重复调用；需要其他周期或其他币时再调用。
- 新闻没有预取，必须用 search_news 检索相关新闻，并查清有效期和持仓时限内的重大数据及公布时间。
- 最后必须调用一次 submit_trade_plan。校验不通过会返回原因，按原因修正后重新提交。

## 程序如何执行
- 计划（action=plan）被采纳后，程序立即在 entry_price 挂限价单（不保证 maker，手续费以实际成交为准），同时在交易所挂只减仓止损（按标记价触发）。到 valid_hours 仍未成交则撤单，并重新请你分析。
- 止损：区间远端外 {rules.STOP_ATR_FACTOR}×ATR14(5m)，做多放在 zone_low 下方，做空放在 zone_high 上方；且距挂单价至少 {rules.MIN_STOP_ATR}×ATR14(5m)，挂单价贴近远端时按这条算。
- 止盈：按成交均价，做多 {_ladder("long")}；做空 {_ladder("short")}。止盈 1 成交后撤销未成交的入场部分，止损移到余仓成本保本价（按实际入场手续费分摊与预计平仓费计算，不用已止盈利润抵扣；资金费和滑点另计）。
- 首次成交后最长持仓 {rules.MAX_HOLD_HOURS} 小时，到时市价平仓。平仓后程序会请你重新分析。
- 仓位：单笔风险为该币交易资金的 {_pct(rules.RISK_PCT)}，置信度低时减半；名义仓位不超过 {rules.MAX_LEVERAGE} 倍该币交易资金。
- reevaluate：任一条件满足时程序重新请你分析（最多 {rules.MAX_REEVALUATE} 个），用来表达另一侧方向、需要重新划区的位置。

## 程序的硬性校验
- zone_low < zone_high，entry_price 在区间内。做多 entry_price 低于现价，做空高于现价（限价单要等价格过来，不能一挂就成交）。
- 止损距挂单价不超过 {_pct(rules.MAX_STOP_PCT)}，数量不低于交易所最小下单金额。
- valid_hours ≤ {rules.SETUP_VALID_HOURS}。

## 有持仓时
- 持仓由程序按止损、止盈和持仓时限管理，你只能 keep（继续持有）或 exit（立即市价平仓），不能提交新计划。
- 只有出现与持仓方向明显相反的重大消息或结构破坏时才 exit。
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
    exchange: str
    provider: AiProvider
    effort: str | None
    dry_run: bool
    system: str
    user_text: str
    context: PlanContext


def plan_brief(decision: dict, sizing: dict | None) -> dict:
    """给模型看的计划摘要：只留价格和状态，不带长段文字"""
    brief = {k: decision.get(k) for k in ("action", "summary", "direction", "confidence", "setup", "valid_hours")}
    brief["reevaluate"] = [{k: c[k] for k in ("direction", "price", "timeframe")} for c in decision.get("reevaluate") or []]
    if sizing and "entry" in sizing:
        brief["entry"] = {k: sizing["entry"].get(k) for k in ("price", "stop", "quantity", "risk_at_stop")}
    return brief


def trade_brief(a: TradingAnalysis) -> str:
    # 技术验收成交只有执行参数，没有模型划区，摘要须如实标注缺失的区间。
    setup = a.sizing.get("setup")
    zone = f"区间 {setup['zone_low']}–{setup['zone_high']}" if setup is not None else "区间未记录"
    head = (f"#{a.id} {DIRECTION_LABELS[a.sizing['direction']]}，{zone}，"
            f"挂单 {a.sizing['entry']['price']}，{local(a.first_fill_at)} 成交")
    if a.plan_status != "closed":
        return f"{head}，{'持仓中' if a.plan_status == 'open' else '平仓中'}"
    return f"{head}，{local(a.closed_at)} {CLOSE_REASON_LABELS.get(a.close_reason, a.close_reason)}，净 {Decimal(a.result['net']):.2f} USDT"


async def recent_atr(symbol: str) -> Decimal:
    """最近已收盘 5m K 线的 ATR14，用于计算止损"""
    bars = parse_bars(await market_client.get("um", "klines", {"symbol": symbol, "interval": rules.ATR_INTERVAL, "limit": rules.ATR_PERIOD + 2}))
    now_ms = datetime.now(UTC).timestamp() * 1000
    return atr([b for b in bars if b.close_time < now_ms])


async def prefetch(symbol: str) -> str:
    """预取行情：工具出错时把错误原样放进消息，不中断分析"""
    period, limit = DERIVATIVES_PARAMS
    calls = [("get_ticker", {"symbol": symbol})]
    calls += [("get_klines", {"symbol": symbol, "interval": i, "limit": 200}) for i in KLINE_INTERVALS]
    calls.append(("get_derivatives", {"symbol": symbol, "period": period, "limit": limit}))
    outcomes = await asyncio.gather(*(run_tool(name, args) for name, args in calls))
    return "\n\n".join(f"### {name} {json.dumps(args, ensure_ascii=False)}\n{o.result}" for (name, args), o in zip(calls, outcomes, strict=True))


def position_side(snapshot: dict, symbol: str) -> str | None:
    position = next((p for p in snapshot["positions"] if p["symbol"] == symbol), None)
    if position is None:
        return None
    return "long" if position["side"] == "LONG" else "short"


async def prepare(symbol: str, reasons: list[str], analysis_id: int) -> Prepared:
    async with SessionLocal() as session:
        account = await session.get(TradingAccount, 1)
        target = await session.get(TradingSymbol, symbol)
        if target is None:
            raise AnalysisError(f"{symbol} 已不在交易列表中")
        if account is None:
            raise AnalysisError("还没有填写模拟盘 key")
        if target.capital_usdt is None:
            raise AnalysisError(f"{symbol} 还没有设置交易资金")
        provider = await session.get(AiProvider, account.provider_id) if account.provider_id else await session.scalar(
            select(AiProvider).where(AiProvider.is_default)
        )
        if provider is None:
            raise AnalysisError("没有可用的模型配置，请先到「环境变量」页面添加")
        session.expunge(provider)
        exchange = account.exchange
        recent = (await session.scalars(
            select(TradingAnalysis)
            .where(TradingAnalysis.exchange == exchange, TradingAnalysis.symbol == symbol, TradingAnalysis.status == "done", TradingAnalysis.id != analysis_id)
            .order_by(TradingAnalysis.id.desc()).limit(RECENT_DECISIONS)
        )).all()
        trades = (await session.scalars(
            select(TradingAnalysis)
            .where(TradingAnalysis.exchange == exchange, TradingAnalysis.symbol == symbol, TradingAnalysis.dry_run.is_(False),
                   TradingAnalysis.first_fill_at >= datetime.now(UTC) - timedelta(hours=RECENT_TRADES_HOURS))
            .order_by(TradingAnalysis.first_fill_at.desc())
        )).all()
        current = await session.scalar(
            select(TradingAnalysis)
            .where(TradingAnalysis.exchange == exchange, TradingAnalysis.symbol == symbol, TradingAnalysis.plan_status.in_(("pending", "open", "closing")), TradingAnalysis.dry_run.is_(False))
            .order_by(TradingAnalysis.id.desc()).limit(1)
        )
        fills = [
            {"purpose": o.purpose, "leg": o.leg, "filled": str(o.filled_qty), "avg_price": str(o.avg_price), "status": o.status}
            for o in (await session.scalars(
                select(TradingOrder).where(TradingOrder.analysis_id == current.id, TradingOrder.filled_qty > 0).order_by(TradingOrder.id)
            )).all()
        ] if current else []
        system = "\n\n".join([INSTRUCTIONS, "## 数据概况", await data_overview(session), await skills_section(session)])
        dry_run = not target.running
        capital_usdt = target.capital_usdt
        effort = account.reasoning_effort or provider.reasoning_effort

    try:
        snapshot = require_fresh_snapshot(await read_snapshot())
    except SnapshotError as e:
        raise AnalysisError(f"无法计算仓位：{e}") from e
    if snapshot["exchange"] != exchange:
        raise AnalysisError("分析期间切换了交易所，请重新分析")
    equity = Decimal(snapshot["account"]["margin_balance"])
    capital = min(capital_usdt, equity)
    position = next((p for p in snapshot["positions"] if p["symbol"] == symbol), None)
    symbol_rules_ = await symbol_rules(symbol, exchange)
    mark = await mark_price(symbol, exchange)
    context = PlanContext(
        mark_price=mark, capital=capital, rules=symbol_rules_, atr=await recent_atr(symbol),
        has_plan=current is not None, position_side=position_side(snapshot, symbol),
    )

    orders = [o for o in snapshot["orders"] + snapshot["algo_orders"] if o["symbol"] == symbol]
    risk = capital * rules.RISK_PCT
    lines = [
        "## 任务",
        f"- 币种：{symbol}（币安 U 本位永续）",
        f"- 触发原因：{'；'.join(reasons)}",
        f"- 运行模式：{'试运行（该币未在交易，计划只记录、不下单）' if dry_run else '交易中（计划会在模拟盘执行）'}",
        "",
        "## 账户（程序据此计算数量）",
        f"- 该币交易资金：{capital:.2f} USDT（设定 {capital_usdt} USDT，账户权益 {equity:.2f} USDT，取较小者）",
        f"- 本次单笔风险金额：{risk:.2f} USDT（{_pct(rules.RISK_PCT)}；置信度低时 {risk * rules.LOW_CONFIDENCE_FACTOR:.2f}）",
        f"- 名义仓位上限：{capital * rules.MAX_LEVERAGE:.2f} USDT（{rules.MAX_LEVERAGE} 倍该币交易资金）",
        f"- 模拟盘交易规则：价格精度 {symbol_rules_.tick_size}，数量精度 {symbol_rules_.step_size}，最小下单金额 {symbol_rules_.min_notional} USDT",
        f"- 执行交易所：{exchange} 模拟盘；行情研究数据来自币安实盘，跨交易所价格可能有差异",
        f"- 模拟盘标记价：{mark}（下单校验以此为准）",
        f"- 最近已收盘 5m K 线的 ATR14：{context.atr:.{max(0, -symbol_rules_.tick_size.as_tuple().exponent)}f}（程序按它计算止损）",
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
            "fills": fills,
            **plan_brief(current.decision, current.sizing),
        }) if current else "无",
        "",
        "## 最近决策（新的在前）",
        *([f"- {local(a.started_at)}{'（试运行）' if a.dry_run else ''}：{dump(plan_brief(a.decision, a.sizing))}" for a in recent] or ["无"]),
        "",
        f"## 近 {RECENT_TRADES_HOURS} 小时已成交的计划（新的在前，共 {len(trades)} 次开仓）",
        *([f"- {trade_brief(a)}" for a in trades] or ["无"]),
        "",
        "## 预取的行情（实盘数据，已取，不要用相同参数重复调用）",
        await prefetch(symbol),
    ]
    return Prepared(exchange=exchange, provider=provider, effort=effort, dry_run=dry_run, system=system, user_text="\n".join(lines), context=context)


async def complete(prepared: Prepared, messages: list[dict], tools: list[llm.ToolSpec]) -> llm.Completion:
    async for item in llm.stream(prepared.provider, prepared.system, messages, tools, prepared.effort):
        if isinstance(item, llm.Completion):
            return item
    raise AssertionError("llm.stream 没有产出 Completion")


async def add_event(symbol: str, analysis_id: int, kind: str, message: str, level: str = "info", data: dict | None = None) -> None:
    async with SessionLocal() as session:
        analysis = await session.get(TradingAnalysis, analysis_id)
        session.add(TradingEvent(exchange=analysis.exchange, symbol=symbol, analysis_id=analysis_id, kind=kind, level=level, message=message, data=data))
        await session.commit()


def describe(plan: TradePlan, sizing: dict, dry_run: bool) -> str:
    prefix = "试运行" if dry_run else ""
    if plan.action != "plan":
        return f"{prefix}{ACTION_LABELS[plan.action]}：{plan.summary}"
    setup, entry = sizing["setup"], sizing["entry"]
    return (
        f"{prefix}计划：限价{DIRECTION_LABELS[plan.direction]} {entry['price']} × {entry['quantity']}，置信度{CONFIDENCE_LABELS[plan.confidence]}；"
        f"区间 {setup['zone_low']}–{setup['zone_high']}，止损 {entry['stop']}（{entry['stop_pct']}%），止损亏损 {entry['risk_at_stop']} USDT，"
        f"有效 {plan.valid_hours} 小时。{plan.summary}"
    )


async def submit(prepared: Prepared, symbol: str, analysis_id: int, arguments: dict) -> tuple[str, tuple[TradePlan, dict] | None]:
    try:
        plan = TradePlan.model_validate(arguments)
    except ValidationError as e:
        return dump({"error": "参数无效", "details": e.errors(include_url=False)}), None
    # 分析可能持续几分钟，按提交时的标记价、持仓和 ATR 校验
    prepared.context.mark_price = await mark_price(symbol, prepared.exchange)
    prepared.context.atr = await recent_atr(symbol)
    snapshot = await read_snapshot()
    if snapshot:
        prepared.context.position_side = position_side(snapshot, symbol)
    prepared.context.closed_prices = {}
    for interval in sorted({c.timeframe for c in plan.reevaluate if c.timeframe != "mark"}):
        rows = await market_client.get("um", "klines", {"symbol": symbol, "interval": interval, "limit": 2})
        now_ms = datetime.now(UTC).timestamp() * 1000
        closed = rows[-1] if rows[-1][6] < now_ms else rows[-2]
        prepared.context.closed_prices[interval] = Decimal(closed[4])
    try:
        sizing = check_setup(plan, prepared.context)
    except PlanError as e:
        await add_event(symbol, analysis_id, "plan_rejected", f"计划未通过校验，已退回模型修正：{'；'.join(e.errors)}", "warn")
        return dump({"error": "计划未通过校验", "details": e.errors, "mark_price": str(prepared.context.mark_price)}), None
    return dump({"status": "已接受", "sizing": sizing}), (plan, sizing)


async def run_analysis(symbol: str, reasons: list[str]) -> None:
    """一次完整的分析；结果和失败原因都写进 trading_analyses 和时间线，不向外抛出"""
    async with SessionLocal() as session:
        # 串行核验额度并占用名额，防止并发分析或已排队请求越过新上限。
        await session.scalar(select(TradingAccount).where(TradingAccount.id == 1).with_for_update())
        if reasons != ["手动分析"] and await daily_limit_reached(session):
            await session.commit()
            return
        target = await session.get(TradingSymbol, symbol)
        analysis = TradingAnalysis(exchange=await active_exchange(session), symbol=symbol, reasons=reasons, dry_run=not (target and target.running), status="running")
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
