<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

type Fund = {
  kind: string
  code: string
  name: string
  fee: number | null
  tracking_error: number | null
  scale: number | null
  age_days: number | null
  turnover: number | null
  premium: number | null
  status: string
  reason: string | null
}
type IndexRow = {
  name: string
  index_name: string
  category: string | null
  valuation_source: string | null
  pe_from: string | null
  note: string | null
  metric: string
  current: number | null
  percentile: number | null
  earnings_yield: number | null
  gate_yield: number | null
  spread: number | null
  normalized_roe: number | null
  min_roe: number | null
  roe_headroom: number | null
  history_years: number
  dividend_yield: number | null
  roe: number | null
  status: string
  reason: string | null
  fund: Fund
}
type Result = {
  as_of: string
  universe_generated_at: string | null
  treasury_yield: number
  treasury_date: string | null
  coverage: { judged: number; total: number }
  tracking_failures: number
  indices: IndexRow[]
}
type Run = { id: number; status: string; progress: string | null; error: string | null }
type TabKey = 'pass_ok' | 'pass_fund' | 'review' | 'drop' | 'insufficient' | 'all'

const TABS: { key: TabKey; label: string; tone?: string }[] = [
  { key: 'pass_ok', label: '符合低估值', tone: 'pass' },
  { key: 'pass_fund', label: '基金受限', tone: 'warn' },
  { key: 'review', label: '待复核', tone: 'warn' },
  { key: 'drop', label: '不符合' },
  { key: 'insufficient', label: '数据不足' },
  { key: 'all', label: '全部' },
]
const STATUS: Record<string, string> = {
  pass: '符合低估值',
  drop: '不符合',
  review: '待复核',
  insufficient: '数据不足',
}
const FUND_STATUS: Record<string, string> = { pass: '通过', drop: '淘汰', insufficient: '数据不足' }
const TREASURY_STALE_DAYS = 30
const SOURCE: Record<string, string> = { danjuan: '蛋卷', csindex: '中证' }
const CATEGORY_ORDER = ['宽基', '策略', '行业', '主题', '海外', '其他']

const run = ref<Run | null>(null)
const result = ref<Result | null>(null)
const errorText = ref('')
const starting = ref(false)
const staleResult = ref(false)
const tab = ref<TabKey>('pass_ok')
const category = ref('')
let timer: number | undefined

const busy = computed(() => run.value?.status === 'queued' || run.value?.status === 'running')
const asOf = computed(() => (result.value ? new Date(result.value.as_of).toLocaleString('zh-CN') : ''))
const universeDate = computed(() =>
  result.value?.universe_generated_at
    ? new Date(result.value.universe_generated_at).toLocaleDateString('zh-CN')
    : '—',
)
const treasuryStale = computed(() => {
  const day = result.value?.treasury_date
  return day ? (Date.now() - new Date(day).getTime()) / 86_400_000 > TREASURY_STALE_DAYS : false
})
const gateFloor = computed(() =>
  result.value ? (result.value.treasury_yield + 3).toFixed(2) : '—',
)

function matchesTab(row: IndexRow, key: TabKey): boolean {
  if (key === 'all') return true
  if (key === 'pass_ok') return row.status === 'pass' && row.fund.status === 'pass'
  if (key === 'pass_fund') return row.status === 'pass' && row.fund.status !== 'pass'
  return row.status === key
}

const tabCounts = computed(() => {
  const all = result.value?.indices ?? []
  const out: Record<TabKey, number> = {
    pass_ok: 0,
    pass_fund: 0,
    review: 0,
    drop: 0,
    insufficient: 0,
    all: all.length,
  }
  for (const row of all) {
    if (row.status === 'pass') {
      if (row.fund.status === 'pass') out.pass_ok += 1
      else out.pass_fund += 1
    } else if (row.status in out) {
      out[row.status as TabKey] += 1
    }
  }
  return out
})

const tabRows = computed(() =>
  (result.value?.indices ?? []).filter((row) => matchesTab(row, tab.value)),
)

const categories = computed(() => {
  const counts = new Map<string, number>()
  for (const row of tabRows.value) {
    const key = row.category || '其他'
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return [...counts.entries()].sort(
    (a, b) => CATEGORY_ORDER.indexOf(a[0]) - CATEGORY_ORDER.indexOf(b[0]),
  )
})

const rows = computed(() =>
  tabRows.value.filter((row) => !category.value || (row.category || '其他') === category.value),
)

function selectTab(key: TabKey) {
  tab.value = key
  if (category.value && !categories.value.some(([k]) => k === category.value)) {
    category.value = ''
  }
}

function fmt(value: number | null | undefined, digits = 2): string {
  return value == null ? '—' : value.toFixed(digits)
}

function pctTone(percentile: number | null): string {
  if (percentile == null) return ''
  if (percentile <= 20) return 'pct-low'
  if (percentile <= 50) return 'pct-mid'
  return 'pct-high'
}

function roeNote(row: IndexRow): string {
  if (row.metric !== 'pb' || row.normalized_roe == null || row.min_roe == null) return ''
  const span = row.roe_headroom != null && row.roe_headroom > 0 ? `，可降 ${fmt(row.roe_headroom, 0)}%` : ''
  return `正常化 ROE ${fmt(row.normalized_roe, 1)}%（底线 ${fmt(row.min_roe, 1)}%${span}）`
}

function spreadText(row: IndexRow): string {
  if (row.spread == null) return ''
  const sign = row.spread >= 0 ? '+' : ''
  return `利差 ${sign}${fmt(row.spread, 1)}%`
}

function stopPoll() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

async function load() {
  try {
    const response = await fetch('/api/index-funds/latest')
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '无法读取指数基金结果'
      return
    }
    run.value = data.run
    const usable = data.result && data.result.coverage && Array.isArray(data.result.indices)
    result.value = usable ? data.result : null
    staleResult.value = Boolean(data.result) && !usable
    if (busy.value && timer === undefined) {
      timer = window.setInterval(() => void load(), 3000)
    } else if (!busy.value) {
      stopPoll()
    }
  } catch {
    errorText.value = '无法读取指数基金结果'
  }
}

async function refresh() {
  if (busy.value || starting.value) {
    return
  }
  starting.value = true
  errorText.value = ''
  try {
    const response = await fetch('/api/index-funds/refresh', { method: 'POST' })
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '刷新失败'
    }
    await load()
  } catch {
    errorText.value = '刷新失败'
  } finally {
    starting.value = false
  }
}

onMounted(load)
onBeforeUnmount(stopPoll)
</script>

<template>
  <section class="index-funds">
    <div class="toolbar">
      <h1>指数基金</h1>
      <button type="button" :disabled="busy || starting" @click="refresh">
        {{ busy ? '刷新中…' : '刷新数据' }}
      </button>
    </div>

    <p v-if="busy">状态：{{ run?.progress || '排队中' }}</p>
    <p v-if="run?.status === 'failed'" class="thesis-fail">上次刷新失败：{{ run.error }}</p>
    <p v-if="errorText" class="thesis-fail">{{ errorText }}</p>
    <p v-if="staleResult && !busy">上次结果是旧版格式，规则已更新，请点“刷新数据”重新生成。</p>
    <p v-else-if="!result && !busy">还没有数据，点“刷新数据”开始。</p>

    <template v-if="result">
      <div class="meta-bar">
        <span>
          十年国债 <strong>{{ result.treasury_yield }}%</strong>（{{ result.treasury_date || '日期未知' }}，达标收益率 ≥ <strong>{{ gateFloor }}%</strong>）
        </span>
        <span>估值覆盖 <strong>{{ result.coverage.judged }} / {{ result.coverage.total }}</strong></span>
        <span class="muted">更新于 {{ asOf }} · 名单 {{ universeDate }}</span>
        <span v-if="result.tracking_failures > 0" class="warn-inline">
          跟踪误差缺失 {{ result.tracking_failures }} 只
        </span>
      </div>
      <p v-if="treasuryStale" class="warn-banner">
        国债收益率已超过 {{ TREASURY_STALE_DAYS }} 天未更新，利差判定已暂停（改判待复核）。
      </p>

      <details class="fold rule-fold">
        <summary>规则与口径说明</summary>
        <p class="muted">
          这是低估值指数的发现器，不是买卖信号。每个指数只留一只费率最低、规模与跟踪合格的代表基金，走势与更大指数高度重合的已合并。
          “符合低估值”指估值收益率（PE 口径为 100÷PE，周期与金融类为正常化 ROE÷PB）比十年国债高 3 个百分点以上，不代表预期回报。
          同一结论内按各自约 10 年历史分位由低到高排列；分位只说明相对自身历史的位置，不能跨指数比较价值。
        </p>
      </details>

      <div class="filter-bar">
        <div class="pill-group" role="tablist" aria-label="筛选结论">
          <button
            v-for="item in TABS"
            :key="item.key"
            type="button"
            class="pill"
            :class="[
              item.tone ? `pill-${item.tone}` : '',
              { active: tab === item.key },
            ]"
            @click="selectTab(item.key)"
          >
            {{ item.label }}
            <span class="pill-count">{{ tabCounts[item.key] }}</span>
          </button>
        </div>

        <div v-if="categories.length > 1" class="pill-group category-pills" aria-label="指数类别">
          <button
            type="button"
            class="pill pill-sm"
            :class="{ active: category === '' }"
            @click="category = ''"
          >
            全部类别
          </button>
          <button
            v-for="[key, count] in categories"
            :key="key"
            type="button"
            class="pill pill-sm"
            :class="{ active: category === key }"
            @click="category = category === key ? '' : key"
          >
            {{ key }}
            <span class="pill-count">{{ count }}</span>
          </button>
        </div>
      </div>

      <div class="book-wrap">
        <table class="index-table">
          <thead>
            <tr>
              <th>指数</th>
              <th class="num">当前估值</th>
              <th class="num">历史分位</th>
              <th class="num">估值收益率</th>
              <th class="num">股息率</th>
              <th>代表基金</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="rows.length === 0">
              <td colspan="6" class="empty-row muted">当前筛选条件下没有指数。</td>
            </tr>
            <tr v-for="row in rows" :key="row.index_name">
              <td class="col-index">
                <div class="index-head">
                  <strong class="index-title" :title="row.index_name">{{ row.name }}</strong>
                  <span class="cat-badge">{{ row.category || '其他' }}</span>
                  <span
                    v-if="tab === 'all'"
                    class="status-badge"
                    :class="`status-${row.status}`"
                  >
                    {{ STATUS[row.status] || row.status }}
                  </span>
                </div>
                <div v-if="roeNote(row)" class="sub-note roe-meta">
                  {{ roeNote(row) }}
                </div>
                <div v-if="row.reason" class="sub-note" :class="row.status === 'review' ? 'text-warn' : 'text-muted'">
                  {{ row.reason }}
                </div>
                <div v-if="row.note" class="sub-note text-warn">
                  {{ row.note }}
                </div>
              </td>

              <td class="num col-val">
                <template v-if="row.current != null">
                  <div class="val-main">
                    <span class="metric-label">{{ row.metric.toUpperCase() }}</span>
                    <strong>{{ fmt(row.current) }}</strong>
                  </div>
                  <div
                    class="sub-meta"
                    :title="row.pe_from ? `中证 ${row.pe_from} 的 PE，按指数此后涨跌推算` : ''"
                  >
                    <span>{{ row.valuation_source ? SOURCE[row.valuation_source] : '' }}</span>
                    <span v-if="row.pe_from" class="proxy-badge">推算</span>
                  </div>
                </template>
                <span v-else class="muted">—</span>
              </td>

              <td
                class="num col-pct"
                :class="pctTone(row.percentile)"
                :title="row.percentile == null ? '' : `基于约 ${row.history_years} 年周度数据`"
              >
                <template v-if="row.percentile != null">
                  <div class="pct-value">{{ fmt(row.percentile, 1) }}%</div>
                  <div class="pct-track">
                    <i :style="{ width: `${Math.min(100, Math.max(2, row.percentile))}%` }" />
                  </div>
                </template>
                <span v-else class="muted">—</span>
              </td>

              <td class="num col-yield" :title="roeNote(row) || '100 ÷ PE'">
                <template v-if="row.gate_yield != null">
                  <div
                    class="yield-main"
                    :class="{
                      'yield-strong': row.spread != null && row.spread >= 5,
                      'yield-pass': row.spread != null && row.spread >= 3 && row.spread < 5,
                    }"
                  >
                    {{ fmt(row.gate_yield) }}%
                  </div>
                  <div v-if="spreadText(row)" class="sub-meta">{{ spreadText(row) }}</div>
                </template>
                <span v-else class="muted">—</span>
              </td>

              <td
                class="num col-div"
                :class="{ 'div-high': row.dividend_yield != null && row.dividend_yield >= 3.5 }"
              >
                {{ row.dividend_yield == null ? '—' : fmt(row.dividend_yield) + '%' }}
              </td>

              <td class="col-fund">
                <div class="fund-head">
                  <span
                    class="kind-badge"
                    :class="row.fund.kind === '场内' ? 'kind-etf' : 'kind-otc'"
                  >
                    {{ row.fund.kind }}
                  </span>
                  <span class="fund-title">{{ row.fund.name }}</span>
                  <code class="fund-code">{{ row.fund.code }}</code>
                </div>
                <div class="fund-meta">
                  <span :class="{ 'fee-low': row.fund.fee != null && row.fund.fee <= 0.25 }">
                    费率 {{ fmt(row.fund.fee) }}%
                  </span>
                  <span>规模 {{ fmt(row.fund.scale, 1) }}亿</span>
                  <span>
                    误差 {{ row.fund.tracking_error == null ? '—' : fmt(row.fund.tracking_error) + '%' }}
                  </span>
                  <span
                    v-if="row.fund.kind === '场内' && row.fund.premium != null"
                    :class="{ 'text-warn': row.fund.premium > 0.5 }"
                  >
                    溢价 {{ fmt(row.fund.premium) }}%
                  </span>
                  <span v-if="row.fund.status !== 'pass'" class="fund-alert">
                    {{ FUND_STATUS[row.fund.status] || row.fund.status }}{{ row.fund.reason ? `：${row.fund.reason}` : '' }}
                  </span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.meta-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1.25rem;
  align-items: center;
  background: #fff;
  border: 1px solid #d8d2c4;
  padding: 0.55rem 0.9rem;
  font-size: 0.9rem;
  margin-bottom: 0.6rem;
}

.warn-banner {
  background: #fdf3e6;
  border: 1px solid #e6c9a2;
  color: #8a4b08;
  padding: 0.5rem 0.85rem;
  margin: 0 0 0.6rem;
  font-size: 0.9rem;
}

.warn-inline {
  color: #8a4b08;
  font-weight: 600;
}

.rule-fold {
  margin: 0 0 0.75rem;
  padding: 0.15rem 0.9rem;
}

.rule-fold summary {
  font-size: 0.9rem;
  color: #5c564c;
  padding: 0.35rem 0;
}

.rule-fold p {
  margin: 0.25rem 0 0.5rem;
}

.filter-bar {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.pill-group {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.32rem 0.7rem;
  border-radius: 3px;
  border: 1px solid #d0c8b8;
  background: #fff;
  color: #3b3730;
  font-size: 0.88rem;
  line-height: 1.2;
}

.pill-sm {
  padding: 0.22rem 0.55rem;
  font-size: 0.82rem;
}

.pill-count {
  font-variant-numeric: tabular-nums;
  font-size: 0.78rem;
  color: #6e675b;
  background: #f1ece2;
  padding: 0.05rem 0.35rem;
  border-radius: 2px;
}

.pill.active {
  background: #2c2925;
  border-color: #2c2925;
  color: #fff;
  font-weight: 600;
}

.pill.active .pill-count {
  background: rgba(255, 255, 255, 0.18);
  color: #fff;
}

.pill-pass.active {
  background: #1b5e4a;
  border-color: #1b5e4a;
}

.pill-warn.active {
  background: #8a4b08;
  border-color: #8a4b08;
}

.index-table {
  border: 1px solid #d8d2c4;
}

.index-table th {
  background: #efeae0;
  color: #464139;
  white-space: nowrap;
  padding: 0.55rem 0.65rem;
  border-bottom: 1px solid #d8d2c4;
}

.index-table td {
  padding: 0.55rem 0.65rem;
  vertical-align: top;
  border-bottom: 1px solid #e8e2d5;
}

.index-table tbody tr:nth-child(even) td {
  background: #faf8f3;
}

.index-table tbody tr:hover td {
  background: #f2ede2;
}

.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.empty-row {
  text-align: center;
  padding: 1.5rem !important;
}

.col-index {
  min-width: 9.5rem;
}

.index-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
}

.index-title {
  color: #1a1a1a;
}

.cat-badge {
  font-size: 0.74rem;
  color: #5c564c;
  background: #efeae0;
  padding: 0.05rem 0.38rem;
  border-radius: 2px;
  white-space: nowrap;
}

.status-badge {
  font-size: 0.74rem;
  padding: 0.05rem 0.38rem;
  border-radius: 2px;
  white-space: nowrap;
}

.status-pass {
  background: #e3f1ec;
  color: #1b5e4a;
}

.status-review {
  background: #fdf3e6;
  color: #8a4b08;
}

.status-drop,
.status-insufficient {
  background: #eee9df;
  color: #5c564c;
}

.sub-note {
  margin-top: 0.2rem;
  font-size: 0.78rem;
  line-height: 1.35;
}

.text-warn {
  color: #8a4b08;
}

.text-muted {
  color: #6e675b;
}

.val-main {
  display: inline-flex;
  align-items: baseline;
  gap: 0.28rem;
}

.metric-label {
  font-size: 0.74rem;
  color: #6e675b;
  font-weight: 600;
}

.sub-meta {
  margin-top: 0.12rem;
  font-size: 0.75rem;
  color: #6e675b;
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 0.25rem;
}

.roe-meta {
  color: #4f4a41;
}

.proxy-badge {
  background: #fdf3e6;
  color: #8a4b08;
  border: 1px solid #ead0ad;
  padding: 0 0.28rem;
  border-radius: 2px;
  font-size: 0.7rem;
}

.col-pct {
  min-width: 5.2rem;
}

.pct-value {
  font-weight: 600;
}

.pct-low .pct-value {
  color: #1b5e4a;
  font-weight: 700;
}

.pct-mid .pct-value {
  color: #2c2925;
}

.pct-high .pct-value {
  color: #6e675b;
  font-weight: 400;
}

.pct-track {
  margin-top: 0.22rem;
  margin-left: auto;
  width: 4.2rem;
  height: 0.28rem;
  background: #ece6da;
  border-radius: 2px;
  overflow: hidden;
}

.pct-track > i {
  display: block;
  height: 100%;
  background: #b8ae9c;
}

.pct-low .pct-track > i {
  background: #1b5e4a;
}

.pct-mid .pct-track > i {
  background: #6f8f7f;
}

.yield-main {
  font-weight: 600;
}

.yield-pass {
  color: #1b5e4a;
}

.yield-strong {
  color: #14523f;
  font-weight: 700;
}

.div-high {
  color: #1b5e4a;
  font-weight: 600;
}

.col-fund {
  min-width: 15rem;
}

.fund-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
}

.kind-badge {
  font-size: 0.72rem;
  font-weight: 600;
  padding: 0.05rem 0.36rem;
  border-radius: 2px;
  white-space: nowrap;
}

.kind-etf {
  background: #e5eef6;
  color: #1a4a7a;
}

.kind-otc {
  background: #f0eadf;
  color: #5a5246;
}

.fund-title {
  font-weight: 600;
  color: #1a1a1a;
}

.fund-code {
  font-size: 0.8rem;
  color: #5c564c;
  font-variant-numeric: tabular-nums;
}

.fund-meta {
  margin-top: 0.18rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.15rem 0.6rem;
  font-size: 0.76rem;
  color: #6e675b;
  font-variant-numeric: tabular-nums;
}

.fee-low {
  color: #1b5e4a;
  font-weight: 600;
}

.fund-alert {
  background: #fbeaea;
  color: #9f1d1d;
  padding: 0 0.35rem;
  border-radius: 2px;
  font-weight: 600;
}
</style>
