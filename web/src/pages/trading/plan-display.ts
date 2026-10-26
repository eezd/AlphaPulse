import type { PlanStatus } from "@@/apis/trading/type"

/** 本程序下的单；其他是用户手动下的 */
export function isOwnOrder(clientId: string) {
  return /^ap\d+$/.test(clientId)
}

export const PLAN_STATUS_LABELS: Record<PlanStatus, string> = {
  proposed: "待采纳",
  pending: "挂单中",
  open: "持仓中",
  closing: "平仓中",
  closed: "已结束",
  canceled: "已撤销",
  replaced: "已被替换",
  stale: "已作废",
  applied: "已执行"
}

export const CLOSE_REASON_LABELS: Record<string, string> = {
  stop: "止损",
  take_profit: "止盈",
  time: "到达持仓时限",
  exit: "模型决定结束",
  external: "外部平仓",
  stop_failed: "止损挂单失败",
  close: "市价平仓",
  manual_close: "一键平仓",
  manual_cancel: "清除挂单",
  // 计划 3.1 之前的旧原因，历史仓位里还会出现
  invalidation: "短线失效条件触发",
  replaced: "被新计划替换",
  expired: "限价单到期未成交",
  stopped: "已停止交易",
  entry_failed: "入场单被拒"
}

/** 止盈阶梯（与后端 plan.TAKE_PROFIT_STEPS 一致）：[距成交均价的涨跌幅, 平仓比例] */
export const TAKE_PROFIT_STEPS: Record<"long" | "short", [number, number][]> = {
  long: [[0.005, 0.6], [0.0075, 0.3], [0.01, 0.1]],
  short: [[0.005, 0.8], [0.0075, 0.2]]
}
