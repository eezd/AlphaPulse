<script setup lang="ts">
import type { Market, QuoteMessage } from "@@/apis/market/type"
import { useMarketSocket } from "../composables/useMarketSocket"

const props = defineProps<{ market: Market, symbol: string }>()
const book = ref<QuoteMessage["data"]>({})
const mark = ref<QuoteMessage["data"]>({})
const trades = ref<QuoteMessage["data"][]>([])
const now = ref(Date.now())
const tab = ref("depth")
const stale = computed(() => !book.value.at || now.value - book.value.at > 15000)
const markStale = computed(() => !mark.value.at || now.value - mark.value.at > 15000)
const { connected, subscribe } = useMarketSocket(() => {}, () => {}, (message) => {
  if (message.type === "depth" && (message.data.at ?? 0) >= (book.value.at ?? 0)) book.value = message.data
  else if (message.type === "mark_price" && (message.data.at ?? 0) >= (mark.value.at ?? 0)) mark.value = message.data
  else if (message.type === "trade" && !trades.value.some(t => t.id === message.data.id)) trades.value = [message.data, ...trades.value].sort((a, b) => (b.time ?? 0) - (a.time ?? 0) || (b.id ?? 0) - (a.id ?? 0)).slice(0, 100)
})
watch(() => [props.market, props.symbol], () => {
  book.value = {}
  mark.value = {}
  trades.value = []
  subscribe({ market: props.market, symbol: props.symbol, interval: "1m", quotes: true })
}, { immediate: true })
const timer = setInterval(() => now.value = Date.now(), 1000)
onBeforeUnmount(() => clearInterval(timer))
const time = (value?: number) => value ? new Date(value).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—"
</script>

<template>
  <aside class="quote-panel">
    <div class="quote-heading">
      实时行情 <el-tag size="small" :type="connected && !stale ? 'success' : 'warning'">
        {{ !connected ? '已断开' : stale ? '等待报价' : '实时' }}
      </el-tag>
    </div>
    <div v-if="market === 'um'" class="quote-funding">
      <span v-if="markStale || !connected" class="text-amber-600">资金数据未就绪或已过期</span>
      <span>资金费率 {{ mark.funding_rate ? `${(+mark.funding_rate * 100).toFixed(4)}%` : '—' }}</span><span>标记 {{ mark.mark_price || '—' }}</span><span>指数 {{ mark.index_price || '—' }}</span><span>距结算 {{ mark.next_funding_at ? `${Math.max(0, Math.floor((mark.next_funding_at - now) / 60000))} 分钟` : '—' }}</span>
    </div>
    <el-tabs v-model="tab">
      <el-tab-pane label="盘口深度" name="depth" /><el-tab-pane label="聚合成交" name="trades" />
    </el-tabs>
    <template v-if="tab === 'depth'">
      <div class="quote-row muted">
        <span>价格</span><span>数量</span>
      </div><div v-for="row in [...(book.asks ?? [])].slice(0, 20).reverse()" :key="row[0]" class="quote-row sell">
        <span>{{ row[0] }}</span><span>{{ row[1] }}</span>
      </div><div class="quote-mid">
        {{ stale ? '报价未就绪或已过期' : '买卖各 20 档 · 实时快照' }}
      </div><div v-for="row in book.bids ?? []" :key="row[0]" class="quote-row buy">
        <span>{{ row[0] }}</span><span>{{ row[1] }}</span>
      </div>
    </template>
    <template v-else>
      <div class="quote-row muted">
        <span>价格／数量</span><span>北京时间</span>
      </div><div v-for="trade in trades" :key="trade.id" class="quote-row" :class="trade.buyer_maker ? 'sell' : 'buy'">
        <span>{{ trade.price }} <small>{{ trade.quantity }}</small></span><span>{{ time(trade.time) }}</span>
      </div><el-empty v-if="!trades.length" description="等待成交" :image-size="40" />
    </template>
  </aside>
</template>

<style scoped>
.quote-panel {
  width: 270px;
  flex-shrink: 0;
  border-left: 1px solid #e2e8f0;
  background: #fff;
  overflow: auto;
  padding: 10px;
  font-size: 11px;
}
.quote-heading {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
}
.quote-funding {
  display: grid;
  gap: 5px;
  margin: 12px 0;
  color: #64748b;
}
.quote-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 3px 4px;
  font-variant-numeric: tabular-nums;
}
.quote-row small {
  color: #64748b;
  margin-left: 4px;
}
.buy {
  color: #059669;
  background: #f0fdf480;
}
.sell {
  color: #dc2626;
  background: #fef2f280;
}
.muted {
  color: #64748b;
}
.quote-mid {
  padding: 10px 4px;
  border-block: 1px solid #e2e8f0;
  color: #64748b;
}
@media (max-width: 1023px) {
  .quote-panel {
    width: 220px;
  }
}
@media (max-width: 760px) {
  .quote-panel {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: min(320px, 90%);
    z-index: 5;
    box-shadow: -8px 0 20px #0f172a15;
  }
  .quote-panel.mobile-hidden {
    display: none;
  }
}
</style>
