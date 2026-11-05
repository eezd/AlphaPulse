import type { SourceInfo } from "../src/common/apis/crawler/type.ts"
import assert from "node:assert/strict"
import { test } from "node:test"
import { sourceStatus } from "../src/pages/crawler/sourceStatus.ts"

test("crawler status distinguishes process health from collection results", () => {
  const source: SourceInfo = {
    code: "test",
    name: "Test",
    region_code: null,
    list_url: "https://example.invalid",
    interval_seconds: 60,
    backfill_hours: 72,
    last_success_at: null,
    last_error: null,
    paused_until: null,
    supports_history: false,
    history_target: null,
    history_position: null,
    history_done: false,
    today_new: 0,
    today_revised: 0,
    total_items: 0,
    enabled: true,
    consecutive_failures: 0,
    process: { state: "running", pid: 1, started_at: null, next_run_at: "2026-11-05T00:00:00Z" },
    collection_stats: { round_running: 0 }
  }
  assert.equal(sourceStatus(source).text, "等待调度")
  assert.equal(sourceStatus({ ...source, collection_stats: { round_running: 1 } }).text, "采集中")
  assert.equal(sourceStatus({ ...source, consecutive_failures: 2 }).text, "失败待重试")
  assert.equal(sourceStatus({ ...source, paused_until: "2026-11-06T00:00:00Z" }, false, Date.parse("2026-11-05")).text, "失败退避中")
  assert.equal(sourceStatus({ ...source, collection_stats: { document_gaps: 2 } }).text, "存在文档缺口")
  assert.equal(sourceStatus({ ...source, enabled: false, consecutive_failures: 3 }).text, "已停用")
  assert.equal(sourceStatus({ ...source, configuration_error: "待配置 CRAWLER_CONTACT_EMAIL" }).type, "warning")
  assert.equal(sourceStatus({ ...source, process: null }).text, "采集进程未运行")
})
