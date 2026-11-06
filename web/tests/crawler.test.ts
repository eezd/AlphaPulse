import type { SourceInfo } from "../src/common/apis/crawler/type.ts"
import assert from "node:assert/strict"
import { test } from "node:test"
import { collectionGaps, sourceStatus } from "../src/pages/crawler/sourceStatus.ts"

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
  assert.equal(sourceStatus({ ...source, collection_stats: { document_gaps: 2 } }).text, "存在采集缺口")
  const gapStats: Record<string, number>[] = [{ attachment_gaps: 1 }, { unresolved_bills: 8 }]
  for (const collection_stats of gapStats) {
    assert.equal(sourceStatus({ ...source, collection_stats }).text, "存在采集缺口")
    assert.equal(sourceStatus({ ...source, collection_stats: { ...collection_stats, round_running: 1 } }).text, "采集中")
  }
  assert.equal(sourceStatus({ ...source, enabled: false, consecutive_failures: 3 }).text, "已停用")
  assert.equal(sourceStatus({ ...source, configuration_error: "待配置 CRAWLER_CONTACT_EMAIL" }).type, "warning")
  assert.equal(sourceStatus({ ...source, process: null }).text, "采集进程未运行")
})

test("gap details retain reasons, exhausted state and legacy records while collecting", () => {
  const source = {
    collection_stats: { round_running: 1 },
    history_cursor: {
      bill_retries: {
        "https://api.congress.gov/v3/bill/119/hr/1": { document: { url: "https://api.congress.gov/v3/bill/119/hr/1" }, attempts: 3, next_at: 1 }
      },
      attachment_retries: {
        photo: { document: { url: "https://www.justice.gov/api/release", canonical: "https://www.justice.gov/release" }, attempts: 2, next_at: 1793854270, error: "官方图片附件尚未提取文字，请核对原文" }
      },
      document_retries: {
        body: { document: { url: "https://www.justice.gov/api/body" }, attempts: 1, next_at: 1793853098, error: "HTTP 503" }
      }
    }
  } satisfies Partial<SourceInfo>
  const gaps = collectionGaps(source)
  assert.equal(gaps.length, 3)
  assert.equal(gaps[0].error, "HTTP 503")
  assert.equal(gaps[0].nextAt, new Date(1793853098 * 1000).toISOString())
  assert.equal(gaps[1].url, "https://www.justice.gov/release")
  assert.match(gaps[1].error, /图片/)
  assert.equal(gaps[1].ignored, true)
  assert.equal(gaps[0].ignored, false)
  assert.equal(gaps[1].exhausted, false)
  assert.equal(gaps[2].exhausted, true)
  assert.equal(gaps[2].url, "https://www.congress.gov/bill/119th-congress/house-bill/1")
  assert.match(gaps[2].error, /历史记录未保存具体原因/)
  assert.equal(source.history_cursor.bill_retries["https://api.congress.gov/v3/bill/119/hr/1"].attempts, 3)
  assert.deepEqual(collectionGaps({}), [])
})

test("Congress gap links use public bill pages for all supported bill types", () => {
  const types = {
    hr: "house-bill",
    s: "senate-bill",
    hjres: "house-joint-resolution",
    sjres: "senate-joint-resolution",
    hconres: "house-concurrent-resolution",
    sconres: "senate-concurrent-resolution",
    hres: "house-resolution",
    sres: "senate-resolution"
  }
  for (const [kind, label] of Object.entries(types)) {
    const url = `https://api.congress.gov/v3/bill/119/${kind}/1071?format=json`
    for (const group of ["bill_retries", "attachment_retries", "document_retries"] as const) {
      const source = { history_cursor: { [group]: { [url]: { document: { url }, attempts: 3, next_at: 1 } } } }
      assert.equal(collectionGaps(source)[0].url, `https://www.congress.gov/bill/119th-congress/${label}/1071`)
      assert.equal(source.history_cursor[group][url].document.url, url)
    }
  }
})

test("gap links preserve canonical pages and do not rewrite unrelated URLs", () => {
  for (const url of [
    "https://www.congress.gov/bill/119th-congress/house-bill/1",
    "https://www.justice.gov/release",
    "https://api.congress.gov/v3/committee-meeting/119/house/1",
    "https://api.congress.gov.example.com/v3/bill/119/hr/1"
  ]) {
    const source = { history_cursor: { bill_retries: { old: { document: { url: "https://api.congress.gov/v3/bill/119/hr/1", canonical: url }, attempts: 3, next_at: 1 } } } }
    assert.equal(collectionGaps(source)[0].url, url)
  }
})

test("retired gaps retain evidence without an active warning", () => {
  const source = {
    enabled: true,
    consecutive_failures: 0,
    code: "doj",
    name: "DOJ",
    region_code: "US",
    list_url: "https://www.justice.gov/",
    interval_seconds: 60,
    backfill_hours: 72,
    last_success_at: null,
    last_error: null,
    paused_until: null,
    supports_history: true,
    history_target: null,
    history_position: null,
    history_done: false,
    today_new: 0,
    today_revised: 0,
    total_items: 0,
    process: { state: "running", pid: 1, started_at: null, next_run_at: "2026-11-05T00:00:00Z" },
    collection_stats: { active_gaps: 0, document_gaps: 1, retired_count: 1 },
    history_cursor: { document_retries: { old: { document: { url: "https://www.justice.gov/old" }, attempts: 3, next_at: 1, error: "HTTP 503", retired_at: "2026-11-05T00:00:00Z" } } }
  } as SourceInfo
  assert.equal(sourceStatus(source).text, "等待调度")
  assert.equal(collectionGaps(source)[0].retired, true)
  assert.equal(collectionGaps(source)[0].error, "HTTP 503")
})
