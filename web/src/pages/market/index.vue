<script setup lang="ts">
import { downloadCsv } from "@@/utils/export"
import { DRAW_TOOLS, INTERVAL_LABELS, MAIN_INDICATORS, SUB_INDICATORS } from "./chart"
import AddSymbolDialog from "./components/AddSymbolDialog.vue"
import QuotePanel from "./components/QuotePanel.vue"
import SymbolManager from "./components/SymbolManager.vue"
import { useMarketChart } from "./composables/useMarketChart"

const { symbols, selection, interval, favorites, mainIndicators, subIndicators, indicatorParams, candleType, chartEl, chart, current, dataInterval, intervals, backfillProgress, ticker, tickerReady, priceColor, connected, selectSymbol, toggleFavorite, refreshSymbols, onAdded, formatNumber, formatBig } = useMarketChart()
const quotesVisible = ref(false)
async function fullscreen() {
  if (document.fullscreenElement) await document.exitFullscreen()
  // 弹出菜单默认挂载到 body，整个文档全屏才能保持可见和可点击。
  else await document.documentElement.requestFullscreen()
}
const addVisible = ref(false)
const managerVisible = ref(false)
const editingFavorites = ref(false)
function screenshot() {
  const image = chart.value?.getConvertPictureUrl(true, "png", "#ffffff")
  if (!image) return
  const a = document.createElement("a")
  a.href = image
  a.download = `${selection.value.symbol}.png`
  a.click()
}
function exportKlines() {
  if (!chart.value) return
  const range = chart.value.getVisibleRange()
  const rows = chart.value.getDataList().slice(Math.max(0, Math.floor(range.from)), Math.ceil(range.to))
  if (!rows.length) {
    ElMessage.warning("当前范围暂无 K 线数据")
    return
  }
  downloadCsv(`${selection.value.symbol}-${interval.value}.csv`, [["时间（UTC）", "开盘", "最高", "最低", "收盘", "成交量"], ...rows.map(r => [new Date(r.timestamp).toISOString(), r.open, r.high, r.low, r.close, r.volume])])
}
</script>

<template>
  <div class="market h-full min-h-120 flex flex-col overflow-hidden rounded-lg bg-[#ffffff] text-[#0f172a]">
    <!-- 顶栏：交易对与 24h 行情 -->
    <div class="flex flex-wrap items-center gap-6 border-b border-b-solid border-[#e2e8f0] px-4 py-2">
      <el-select
        :model-value="`${selection.market}:${selection.symbol}`" filterable class="symbol-select w-52"
        @change="selectSymbol"
      >
        <el-option-group v-for="market in (['spot', 'um'] as const)" :key="market" :label="market === 'spot' ? '现货' : 'U 本位永续'">
          <el-option
            v-for="s in symbols.filter(x => x.market === market)" :key="s.id"
            :value="`${s.market}:${s.symbol}`" :label="`${s.base}/${s.quote}${s.market === 'um' ? ' 永续' : ''}`"
          >
            <span>{{ s.base }}/{{ s.quote }}</span>
            <span v-if="s.status !== 'ready'" class="float-right text-xs text-[#2563eb]">{{ s.status === "error" ? "出错" : "补齐中" }}</span>
          </el-option>
        </el-option-group>
      </el-select>
      <div class="text-xl font-bold" :style="{ color: priceColor }">
        {{ current && tickerReady ? formatNumber(ticker.last, current.price_precision) : "-" }}
      </div>
      <div class="stat">
        <div>24h涨跌</div>
        <div :style="{ color: priceColor }">
          {{ tickerReady ? `${ticker.changePercent >= 0 ? "+" : ""}${ticker.changePercent.toFixed(2)}%` : "—" }}
        </div>
      </div>
      <div class="stat">
        <div>24h最高</div>
        <div>{{ current && tickerReady ? formatNumber(ticker.high, current.price_precision) : "-" }}</div>
      </div>
      <div class="stat">
        <div>24h最低</div>
        <div>{{ current && tickerReady ? formatNumber(ticker.low, current.price_precision) : "-" }}</div>
      </div>
      <div class="stat">
        <div>24h成交量({{ current?.base }})</div>
        <div>{{ tickerReady ? formatBig(ticker.volume) : "—" }}</div>
      </div>
      <div class="stat">
        <div>24h成交额({{ current?.quote }})</div>
        <div>{{ tickerReady ? formatBig(ticker.quoteVolume) : "—" }}</div>
      </div>
      <div class="flex-1" />
      <el-button size="small" @click="fullscreen">
        全屏
      </el-button><el-button size="small" class="mobile-quotes-toggle" @click="quotesVisible = !quotesVisible">
        {{ quotesVisible ? "关闭盘口" : "盘口／成交" }}
      </el-button>
      <el-button size="small" @click="screenshot">
        截图
      </el-button><el-button size="small" @click="exportKlines">
        导出可见 K 线
      </el-button>
      <span class="text-xs" :class="connected ? 'text-[#16a34a]' : 'text-[#dc2626]'">● {{ !connected ? "断开" : tickerReady ? "实时" : "等待行情" }}</span>
      <el-button size="small" type="warning" @click="addVisible = true">
        新增交易对
      </el-button>
      <el-button size="small" @click="managerVisible = true">
        管理
      </el-button>
    </div>

    <!-- 周期与指标工具栏 -->
    <div class="flex flex-wrap items-center gap-1 border-b border-b-solid border-[#e2e8f0] px-3 py-1 text-sm">
      <span
        v-for="i in favorites.filter(f => intervals.includes(f))" :key="i"
        class="tool" :class="{ active: interval === i }" @click="interval = i"
      >{{ INTERVAL_LABELS[i] }}</span>
      <el-popover placement="bottom-start" :width="300" trigger="click">
        <template #reference>
          <span class="tool" :class="{ active: !favorites.includes(interval) }">
            {{ favorites.includes(interval) ? "更多" : INTERVAL_LABELS[interval] }} ▾
          </span>
        </template>
        <div class="mb-2 flex items-center justify-between text-xs">
          <span>选择周期</span>
          <el-link type="primary" underline="never" @click="editingFavorites = !editingFavorites">
            {{ editingFavorites ? "完成" : "编辑常用" }}
          </el-link>
        </div>
        <div class="grid grid-cols-4 gap-1">
          <el-button
            v-for="i in intervals" :key="i" size="small" class="ml-0!"
            :type="editingFavorites ? (favorites.includes(i) ? 'warning' : 'default') : (interval === i ? 'primary' : 'default')"
            @click="editingFavorites ? toggleFavorite(i) : (interval = i)"
          >
            {{ INTERVAL_LABELS[i] }}
          </el-button>
        </div>
      </el-popover>
      <el-divider direction="vertical" />
      <el-popover placement="bottom-start" :width="360" trigger="click">
        <template #reference>
          <span class="tool">指标 ▾</span>
        </template>
        <div class="mb-1 text-xs text-[var(--el-text-color-secondary)]">
          主图
        </div>
        <el-checkbox-group v-model="mainIndicators" size="small">
          <el-checkbox-button v-for="name in MAIN_INDICATORS" :key="name" :value="name">
            {{ name }}
          </el-checkbox-button>
        </el-checkbox-group>
        <div class="mb-1 mt-3 text-xs text-[var(--el-text-color-secondary)]">
          副图
        </div>
        <el-checkbox-group v-model="subIndicators" size="small">
          <el-checkbox-button v-for="name in SUB_INDICATORS" :key="name" :value="name" class="mb-1">
            {{ name }}
          </el-checkbox-button>
        </el-checkbox-group>
        <div v-if="mainIndicators.length + subIndicators.length" class="mb-1 mt-3 text-xs text-[var(--el-text-color-secondary)]">
          参数（逗号分隔，留空用默认）
        </div>
        <div v-for="name in [...mainIndicators, ...subIndicators]" :key="name" class="mb-1 flex items-center gap-2">
          <span class="w-12 text-xs">{{ name }}</span>
          <el-input :model-value="indicatorParams[name]" size="small" placeholder="默认" @change="(v: string) => indicatorParams[name] = v" />
        </div>
      </el-popover>
      <el-popover placement="bottom-start" :width="200" trigger="click">
        <template #reference>
          <span class="tool">设置 ▾</span>
        </template>
        <el-radio-group v-model="candleType" size="small" class="flex flex-col items-start">
          <el-radio value="candle_solid">
            实心蜡烛
          </el-radio>
          <el-radio value="candle_stroke">
            空心蜡烛
          </el-radio>
          <el-radio value="candle_up_stroke">
            涨空心
          </el-radio>
          <el-radio value="ohlc">
            美国线
          </el-radio>
        </el-radio-group>
      </el-popover>
      <div class="flex-1" />
      <span v-if="backfillProgress !== null" class="text-xs text-[#2563eb]">
        {{ INTERVAL_LABELS[dataInterval] }} 历史数据补齐中 {{ backfillProgress }}%，最新部分暂未完整
      </span>
    </div>

    <div class="relative min-h-0 flex flex-1">
      <!-- 画线工具栏 -->
      <div class="flex flex-col gap-1 border-r border-r-solid border-[#e2e8f0] p-1 text-xs">
        <span v-for="tool in DRAW_TOOLS" :key="tool.name" class="tool text-center" :title="tool.label" @click="chart?.createOverlay(tool.name)">
          {{ tool.label }}
        </span>
        <span class="tool text-center text-[#dc2626]" title="清除全部画线" @click="chart?.removeOverlay()">清除</span>
      </div>
      <div ref="chartEl" class="min-w-0 flex-1" /><QuotePanel :class="{ 'mobile-hidden': !quotesVisible }" :market="selection.market" :symbol="selection.symbol" />
    </div>

    <AddSymbolDialog v-model="addVisible" @added="onAdded" />
    <SymbolManager v-model="managerVisible" :symbols="symbols" @changed="refreshSymbols" />
  </div>
</template>

<style scoped>
.mobile-quotes-toggle {
  display: none;
}
@media (max-width: 760px) {
  .mobile-quotes-toggle {
    display: inline-flex;
  }
}
.stat {
  font-size: 12px;
}
.stat > div:first-child {
  color: #64748b;
}
.tool {
  padding: 2px 8px;
  border-radius: 4px;
  color: #64748b;
  cursor: pointer;
  white-space: nowrap;
}
.tool:hover {
  color: #0f172a;
  background: #e2e8f0;
}
.tool.active {
  color: #2563eb;
}
.symbol-select :deep(.el-select__wrapper) {
  background: #e2e8f0;
  box-shadow: none;
}
.symbol-select :deep(.el-select__selected-item) {
  color: #0f172a;
  font-weight: 600;
}
</style>
