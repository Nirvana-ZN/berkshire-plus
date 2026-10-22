<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { renderMarkdownHtml } from './homeViewModel'

interface PositionRow {
  name: string
  shares: number
  cost: number
  price?: number | null
  overvalued?: boolean
  cheap_bound?: number | null
  rich_bound?: number | null
  live_verdict?: string | null
  market_value?: number | null
  pnl?: number | null
  weight?: number | null
  stale?: boolean
  source?: string | null
  code?: string | null
  target?: number | null
  no_target?: boolean
  deviation?: number | null
  rule_light?: string | null
  thesis_status?: 'current' | 'stale' | 'none' | null
  thesis_research_id?: number | null
  thesis_verdict?: 'pass' | 'fail' | 'gray' | null
  financial_drift?: {
    period: string
    base_period?: string
    reasons?: string[]
    net_income_yoy?: number | null
  } | null
  dynamic_sizing?: {
    role: string
    role_zh: string
    zone: string
    action: string
    suggested_min?: number | null
    suggested_max?: number | null
    role_cap?: number | null
    label: string
  } | null
}

interface RuleRow {
  id: number
  name: string
  type: string
  price: number
  note?: string | null
}

interface RefreshError {
  name?: string
  detail?: string
  error?: string
}

interface ReviewPosition {
  name: string
  action: string
  reason: string
  actual_weight?: number | null
  target_weight?: number | null
  rule_light?: string | null
  short_exam?: boolean
}

interface ReviewAction {
  action: string
  names: string[]
  cash_or_target_link: string
}

interface ReviewRow {
  id: number
  status: string
  error?: string | null
  positions?: ReviewPosition[]
  portfolio_actions?: ReviewAction[]
  markdown?: string
  has_stale?: boolean
}

interface OptItem {
  name: string
  code?: string | null
  research_id?: number | null
  role_zh: string
  current_weight?: number | null
  price?: number | null
  price_source?: string
  cheap_bound?: number | null
  steady_bound?: number | null
  rich_bound?: number | null
  ready?: boolean
  tag: string
  severity: 'pass' | 'warn' | 'fail' | 'neutral'
  target_range: string
  reason: string
}

interface OptimizationData {
  reduce: OptItem[]
  add: OptItem[]
  build: OptItem[]
  sentiment_regime?: {
    score: number
    zone: string
    label: string
    note: string
  } | null
}

const route = useRoute()
const router = useRouter()
const positions = ref<PositionRow[]>([])
const optimization = ref<OptimizationData>({ reduce: [], add: [], build: [] })
const cash = ref<number | null>(null)
const cashWeight = ref<number | null>(null)
const cashTarget = ref<number | null>(null)
const cashNoTarget = ref(true)
const cashDeviation = ref<number | null>(null)
const rebalance = ref<string[]>([])
const loadError = ref('')
const migrateError = ref('')
const migrateDetail = ref('')
const formError = ref('')
const quoteError = ref('')
const targetError = ref('')
const ruleError = ref('')
const warning = ref('')
const refreshErrors = ref<string[]>([])
const submitting = ref(false)
const refreshing = ref(false)
const savingCode = ref(false)
const savingManual = ref(false)
const savingTargets = ref(false)
const savingRule = ref(false)
const deletingRule = ref<number | null>(null)
const reviewing = ref(false)
const researching = ref(false)
const reviewError = ref('')
const researchError = ref('')
const review = ref<ReviewRow | null>(null)
let reviewTimer: number | undefined
const targetForm = ref<Record<string, string>>({ __CASH__: '' })
const bookForm = ref<Record<string, { shares: string; unit_cost: string }>>({})
const cashForm = ref('')
const bookError = ref('')
const savingBook = ref(false)
const reviewDepth = ref<'reuse' | 'eval_first'>('reuse')
const rules = ref<RuleRow[]>([])
const ruleForm = ref({
  name: '',
  type: '加仓上限价',
  price: '',
  note: '',
})

const totalMarketValue = computed(() =>
  positions.value.reduce((acc, row) => acc + (row.market_value ?? 0), 0),
)
const totalCost = computed(() =>
  positions.value.reduce((acc, row) => acc + (row.cost ?? 0), 0),
)
const totalPnl = computed(() =>
  positions.value.reduce((acc, row) => acc + (row.pnl ?? 0), 0),
)
const totalAssets = computed(() => totalMarketValue.value + (cash.value ?? 0))
const totalReturn = computed(() =>
  totalCost.value > 0 ? totalPnl.value / totalCost.value : null,
)

function todayIso(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

const form = ref({
  date: todayIso(),
  direction: 'buy',
  name: '',
  shares: '',
  price: '',
  note: '',
})
const tradeTarget = ref('')
const selectedTradeCode = ref('')
const tradeSuggestions = ref<{ name: string; code: string }[]>([])
const tradeSuggestOpen = ref(false)
let suggestTimer: number | undefined

async function fetchTradeSuggestions(q: string) {
  try {
    const res = await fetch(`/api/instruments/suggest?q=${encodeURIComponent(q)}`)
    if (res.ok) {
      const data = await res.json()
      tradeSuggestions.value = Array.isArray(data.items) ? data.items : []
    }
  } catch {
    tradeSuggestions.value = []
  }
}

function onTradeNameInput() {
  selectedTradeCode.value = ''
  tradeSuggestOpen.value = true
  if (suggestTimer != null) window.clearTimeout(suggestTimer)
  suggestTimer = window.setTimeout(() => {
    void fetchTradeSuggestions(form.value.name)
  }, 150)
}

function pickTradeSuggest(item: { name: string; code: string }) {
  form.value.name = item.name
  selectedTradeCode.value = item.code
  tradeSuggestOpen.value = false
}

function closeTradeSuggest() {
  window.setTimeout(() => {
    tradeSuggestOpen.value = false
  }, 150)
}

const codeForm = ref({
  name: '',
  code: '',
})

const manualForm = ref({
  name: '',
  price: '',
})

function applyBook(data: {
  positions?: PositionRow[]
  cash?: {
    balance?: number
    weight?: number | null
    target?: number | null
    no_target?: boolean
    deviation?: number | null
  }
  error?: string
  detail?: string
  warning?: string
  errors?: RefreshError[]
  rebalance?: string[]
  optimization?: OptimizationData
}) {
  positions.value = data.positions ?? []
  if (data.optimization) {
    optimization.value = data.optimization
  }
  cash.value = data.cash?.balance ?? 0
  cashWeight.value = data.cash?.weight ?? null
  cashTarget.value = data.cash?.target ?? null
  cashNoTarget.value = data.cash?.no_target ?? true
  cashDeviation.value = data.cash?.deviation ?? null
  rebalance.value = data.rebalance ?? []
  if ('error' in data) {
    migrateError.value = data.error ?? ''
  }
  if ('detail' in data) {
    migrateDetail.value = data.detail ?? ''
  }
  warning.value = data.warning ?? ''
  refreshErrors.value = (data.errors ?? []).map((item) => {
    const label = item.name ? `${item.name}：` : ''
    return `${label}${item.detail || item.error || '刷新失败'}`
  })
  const next: Record<string, string> = {}
  const book: Record<string, { shares: string; unit_cost: string }> = {}
  for (const row of positions.value) {
    next[row.name] =
      row.no_target || row.target == null
        ? ''
        : String(Number(Number(row.target).toFixed(4)))
    book[row.name] = {
      shares: String(row.shares),
      unit_cost: formatUnitCost(row.cost, row.shares),
    }
  }
  next.__CASH__ =
    cashNoTarget.value || cashTarget.value == null
      ? ''
      : String(Number(Number(cashTarget.value).toFixed(4)))
  targetForm.value = next
  bookForm.value = book
  cashForm.value = cash.value == null ? '' : String(cash.value)
}

function formatNumber(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return Number(value).toLocaleString('zh-CN', {
    maximumFractionDigits: 4,
    minimumFractionDigits: 0,
  })
}

function formatUnitCost(cost: number, shares: number): string {
  if (!shares || !Number.isFinite(cost) || !Number.isFinite(shares)) return '—'
  const unit = cost / shares
  if (!Number.isFinite(unit)) return '—'
  return String(Number(unit.toFixed(4)))
}

function thesisLabel(row: PositionRow): string {
  const verdict = row.thesis_verdict
  let text = '论文'
  if (verdict === 'pass') text = '通过'
  else if (verdict === 'fail') text = '不通过'
  else if (verdict === 'gray') text = '灰色地带'
  if (row.thesis_status === 'stale') return `${text}·过期`
  return text
}

function thesisClass(row: PositionRow): string {
  if (row.thesis_verdict === 'fail') return 'badge badge-fail thesis-fail'
  if (row.thesis_verdict === 'pass') return 'badge badge-pass thesis-pass'
  if (row.thesis_verdict === 'gray') return 'badge badge-warn'
  return 'badge badge-neutral'
}

function openThesis(row: PositionRow) {
  if (row.thesis_status === 'none' || row.thesis_research_id == null) return
  void router.push(`/research/${row.thesis_research_id}`)
}

function formatWeight(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return `${(value * 100).toFixed(2)}%`
}

function isManualQuote(source: string | null | undefined): boolean {
  return source === 'manual'
}

function ruleLightLabel(light: string | null | undefined): string {
  if (light === 'hit') return '触及'
  if (light === 'clear') return '未触及'
  if (light === 'unknown') return '无现价'
  return '无规则'
}

function ruleLightClass(light: string | null | undefined): string {
  if (light === 'hit') return 'badge badge-fail'
  if (light === 'clear') return 'badge badge-pass'
  if (light === 'unknown') return 'badge badge-warn'
  return 'muted'
}

const ACTION_ZH: Record<string, string> = {
  hold: '持有',
  add: '加仓',
  cut: '减仓',
  sell: '卖出',
  unknown: '无法判断',
  rebalance_note: '再平衡提示',
  rebalance: '再平衡提示',
  再平衡提示: '再平衡提示',
}

const REVIEW_STATUS: Record<string, string> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}

function actionLabel(action: string | null | undefined): string {
  if (!action) return '—'
  return ACTION_ZH[action] || action
}

function actionBadgeClass(action: string | null | undefined): string {
  if (action === 'add') return 'badge badge-pass'
  if (action === 'cut') return 'badge badge-warn'
  if (action === 'sell') return 'badge badge-fail'
  return 'badge badge-neutral'
}

function portfolioActionLabel(action: string | null | undefined): string {
  return actionLabel(action)
}

function applyReview(data: ReviewRow) {
  review.value = data
}

function stopReviewPoll() {
  if (reviewTimer !== undefined) {
    window.clearInterval(reviewTimer)
    reviewTimer = undefined
  }
}

function reviewBusy(status?: string): boolean {
  return status === 'queued' || status === 'running'
}

async function loadReview(id: number) {
  try {
    const response = await fetch(`/api/reviews/${id}`)
    const data = await response.json()
    if (!response.ok) {
      reviewError.value = data.detail || data.error || '无法读取审视'
      stopReviewPoll()
      reviewing.value = false
      return
    }
    applyReview(data)
    if (!reviewBusy(data.status)) {
      stopReviewPoll()
      reviewing.value = false
      try {
        await loadPositions()
      } catch {
        loadError.value = '无法加载持仓'
      }
    }
  } catch {
    reviewError.value = '无法读取审视'
    stopReviewPoll()
    reviewing.value = false
  }
}

async function loadLatestReview() {
  try {
    const response = await fetch('/api/reviews/latest')
    if (response.status === 404) {
      review.value = null
      return
    }
    const data = await response.json()
    if (!response.ok) {
      reviewError.value = data.detail || data.error || '无法读取审视'
      return
    }
    applyReview(data)
    if (reviewBusy(data.status)) {
      reviewing.value = true
      stopReviewPoll()
      reviewTimer = window.setInterval(() => {
        void loadReview(data.id)
      }, 800)
    }
  } catch {
    reviewError.value = '无法读取审视'
  }
}

async function startResearch(row: PositionRow) {
  if (researching.value) return
  researchError.value = ''
  if (!String(row.code || '').trim()) {
    prefillCode(row.name)
    return
  }
  researching.value = true
  try {
    const response = await fetch('/api/research', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: row.name,
        code: row.code,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      researchError.value = data.detail || data.error || '评估失败'
      return
    }
    if (data.id == null) {
      researchError.value = '评估失败'
      return
    }
    await router.push(`/research/${data.id}`)
  } catch {
    researchError.value = '评估失败'
  } finally {
    researching.value = false
  }
}

async function startReview() {
  if (reviewing.value) return
  reviewError.value = ''
  reviewing.value = true
  try {
    const response = await fetch('/api/reviews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ depth: reviewDepth.value }),
    })
    const data = await response.json()
    if (!response.ok) {
      reviewError.value = data.detail || data.error || '审视失败'
      reviewing.value = false
      return
    }
    applyReview({
      id: data.id,
      status: data.status || 'queued',
      positions: [],
      portfolio_actions: [],
    })
    await loadReview(data.id)
    if (reviewBusy(review.value?.status)) {
      stopReviewPoll()
      reviewTimer = window.setInterval(() => {
        void loadReview(data.id)
      }, 800)
    }
  } catch {
    reviewError.value = '审视失败'
    reviewing.value = false
  }
}

async function loadPositions() {
  const response = await fetch('/api/positions')
  if (!response.ok) {
    loadError.value = '无法加载持仓'
    return
  }
  applyBook(await response.json())
}

async function loadRules() {
  const response = await fetch('/api/rules')
  if (!response.ok) {
    ruleError.value = '无法加载规则'
    return
  }
  const data = await response.json()
  rules.value = data.rules ?? []
}

async function saveRule() {
  if (savingRule.value) return
  ruleError.value = ''
  savingRule.value = true
  try {
    const response = await fetch('/api/rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: ruleForm.value.name,
        type: ruleForm.value.type,
        price: ruleForm.value.price,
        note: ruleForm.value.note || undefined,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      ruleError.value = data.detail || data.error || '保存规则失败'
      return
    }
    rules.value = data.rules ?? []
    ruleForm.value = { name: '', type: '加仓上限价', price: '', note: '' }
    if (data.positions) {
      applyBook(data)
    } else {
      await loadPositions()
    }
  } catch {
    ruleError.value = '保存规则失败'
  } finally {
    savingRule.value = false
  }
}

async function deleteRule(id: number) {
  if (deletingRule.value != null) return
  ruleError.value = ''
  deletingRule.value = id
  try {
    const response = await fetch(`/api/rules/${id}`, { method: 'DELETE' })
    const data = await response.json()
    if (!response.ok) {
      ruleError.value = data.detail || data.error || '删除规则失败'
      return
    }
    rules.value = data.rules ?? []
    if (data.positions) {
      applyBook(data)
    } else {
      await loadPositions()
    }
  } catch {
    ruleError.value = '删除规则失败'
  } finally {
    deletingRule.value = null
  }
}

async function submitTrade() {
  if (submitting.value) return
  formError.value = ''
  warning.value = ''
  submitting.value = true
  try {
    const response = await fetch('/api/trades', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        date: form.value.date,
        direction: form.value.direction,
        name: form.value.name,
        shares: form.value.shares,
        price: form.value.price,
        note: form.value.note || undefined,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      formError.value = data.detail || data.error || '记账失败'
      return
    }
    const tradedName = form.value.name.trim()
    const pickedCode = selectedTradeCode.value.trim()
    const desiredTarget = tradeTarget.value.trim()
    applyBook(data)
    if (pickedCode && tradedName) {
      try {
        const instRes = await fetch('/api/instruments', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: tradedName, code: pickedCode }),
        })
        if (instRes.ok) {
          applyBook(await instRes.json())
        }
      } catch {
        // non-blocking auto-bind
      }
    }
    if (desiredTarget && tradedName) {
      targetForm.value[tradedName] = desiredTarget
      await saveTargets()
    }
    form.value = {
      date: todayIso(),
      direction: 'buy',
      name: '',
      shares: '',
      price: '',
      note: '',
    }
    tradeTarget.value = ''
    selectedTradeCode.value = ''
  } catch {
    formError.value = '记账失败'
  } finally {
    submitting.value = false
  }
}

async function refreshQuotes() {
  if (refreshing.value) return
  quoteError.value = ''
  refreshing.value = true
  try {
    const response = await fetch('/api/quotes/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    const data = await response.json()
    if (!response.ok) {
      quoteError.value = data.detail || data.error || '刷新失败'
      refreshErrors.value = []
      return
    }
    applyBook(data)
  } catch {
    quoteError.value = '刷新失败'
  } finally {
    refreshing.value = false
  }
}

async function submitCode() {
  if (savingCode.value) return
  quoteError.value = ''
  savingCode.value = true
  try {
    const response = await fetch('/api/instruments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: codeForm.value.name,
        code: codeForm.value.code,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      quoteError.value = data.detail || data.error || '补代码失败'
      return
    }
    applyBook(data)
    codeForm.value = { name: '', code: '' }
  } catch {
    quoteError.value = '补代码失败'
  } finally {
    savingCode.value = false
  }
}

async function submitManual() {
  if (savingManual.value) return
  quoteError.value = ''
  savingManual.value = true
  try {
    const response = await fetch('/api/quotes/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: manualForm.value.name,
        price: manualForm.value.price,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      quoteError.value = data.detail || data.error || '手工价失败'
      return
    }
    applyBook(data)
    manualForm.value = { name: '', price: '' }
  } catch {
    quoteError.value = '手工价失败'
  } finally {
    savingManual.value = false
  }
}

function rowDirty(row: PositionRow): boolean {
  const edit = bookForm.value[row.name]
  if (!edit) return false
  return edit.shares.trim() !== String(row.shares)
    || edit.unit_cost.trim() !== formatUnitCost(row.cost, row.shares)
}

function cashDirty(): boolean {
  return cashForm.value.trim() !== (cash.value == null ? '' : String(cash.value))
}

async function saveBook() {
  if (savingBook.value) return
  bookError.value = ''
  savingBook.value = true
  try {
    let last: Record<string, unknown> | null = null
    for (const row of positions.value) {
      const edit = bookForm.value[row.name]
      if (!edit || !rowDirty(row)) continue
      const response = await fetch('/api/positions/adjust', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: row.name,
          shares: edit.shares,
          unit_cost: edit.unit_cost,
        }),
      })
      const data = await response.json()
      if (!response.ok) {
        bookError.value = data.detail || data.error || '保存账面失败'
        return
      }
      last = data
    }
    if (cashDirty()) {
      const response = await fetch('/api/positions/cash', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ balance: cashForm.value }),
      })
      const data = await response.json()
      if (!response.ok) {
        bookError.value = data.detail || data.error || '保存现金失败'
        return
      }
      last = data
    }
    if (last) applyBook(last as Parameters<typeof applyBook>[0])
  } catch {
    bookError.value = '保存账面失败'
  } finally {
    savingBook.value = false
  }
}

async function saveTargets() {
  if (savingTargets.value) return
  targetError.value = ''
  savingTargets.value = true
  try {
    const targets: Record<string, string> = {}
    for (const [name, raw] of Object.entries(targetForm.value)) {
      const text = raw.trim()
      if (!text) continue
      targets[name] = text
    }
    const response = await fetch('/api/targets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ targets }),
    })
    const data = await response.json()
    if (!response.ok) {
      targetError.value = data.detail || data.error || '保存目标失败'
      return
    }
    applyBook(data)
  } catch {
    targetError.value = '保存目标失败'
  } finally {
    savingTargets.value = false
  }
}

function rebalanceLabel(name: string): string {
  return name === '__CASH__' ? '现金' : name
}

function queryText(value: unknown): string {
  const raw = Array.isArray(value) ? value[0] : value
  return typeof raw === 'string' ? raw.trim() : ''
}

function prefillTrade(name: string, direction?: 'buy' | 'sell', price?: number | null, code?: string | null) {
  form.value.name = name
  if (direction) form.value.direction = direction
  if (price != null && Number.isFinite(Number(price))) {
    form.value.price = String(Number(price))
  }
  if (code) selectedTradeCode.value = code
  tradeOpen.value = true
}

function optBadgeClass(severity: string): string {
  if (severity === 'fail') return 'badge badge-fail'
  if (severity === 'warn') return 'badge badge-warn'
  if (severity === 'pass') return 'badge badge-pass'
  return 'badge badge-neutral'
}

function prefillCode(name: string) {
  codeForm.value.name = name
  codeOpen.value = true
}

const tradeOpen = ref(false)
const codeOpen = ref(false)

function applyPrefill() {
  const rawName = queryText(route.query.name)
  const rawPrice = queryText(route.query.price)
  if (rawName) {
    form.value.name = rawName
    tradeOpen.value = true
  }
  if (rawPrice) {
    form.value.price = rawPrice
    tradeOpen.value = true
  }
}

onMounted(async () => {
  applyPrefill()
  try {
    await loadPositions()
    await loadRules()
    await loadLatestReview()
  } catch {
    loadError.value = '无法加载持仓'
  }
})

watch(
  () => [route.query.name, route.query.price],
  () => {
    applyPrefill()
  },
)

onBeforeUnmount(stopReviewPoll)
</script>

<template>
  <section class="page-book">
    <div class="toolbar">
      <h1>看仓</h1>
      <button type="button" :disabled="refreshing" @click="refreshQuotes">
        {{ refreshing ? '刷新中…' : '刷新现价' }}
      </button>
      <select v-model="reviewDepth" aria-label="审视深度">
        <option value="reuse">引用论文，无则短体检</option>
        <option value="eval_first">缺论文先评估再审视</option>
      </select>
      <button type="button" class="btn-primary" :disabled="reviewing" @click="startReview">
        {{ reviewing ? '审视中…' : '一键审视' }}
      </button>
    </div>

    <p v-if="loadError" class="alert-error">{{ loadError }}</p>
    <template v-else>
      <p v-if="migrateError" class="alert-error">{{ migrateError }}{{ migrateDetail ? `：${migrateDetail}` : '' }}</p>
      <p v-if="reviewError" class="alert-error">{{ reviewError }}</p>
      <p v-if="researchError" class="alert-error">{{ researchError }}</p>
      <p v-if="quoteError" class="alert-error">{{ quoteError }}</p>
      <p v-for="item in refreshErrors" :key="item" class="alert-warn">{{ item }}</p>

      <div class="stat-grid">
        <div class="stat-card">
          <p class="stat-label">账户总资产</p>
          <p class="stat-value">{{ formatNumber(totalAssets) }}</p>
          <p class="stat-sub">持仓 {{ positions.length }} 只 + 现金</p>
        </div>
        <div class="stat-card">
          <p class="stat-label">持仓总市值</p>
          <p class="stat-value">{{ formatNumber(totalMarketValue) }}</p>
          <p class="stat-sub">成本总额 {{ formatNumber(totalCost) }}</p>
        </div>
        <div class="stat-card">
          <p class="stat-label">现金余额</p>
          <p class="stat-value">{{ formatNumber(cash) }}</p>
          <p class="stat-sub">占比 {{ formatWeight(cashWeight) }}</p>
        </div>
        <div class="stat-card">
          <p class="stat-label">持仓总浮盈亏</p>
          <p class="stat-value" :class="totalPnl > 0 ? 'num-pos' : totalPnl < 0 ? 'num-neg' : ''">
            {{ totalPnl > 0 ? '+' : '' }}{{ formatNumber(totalPnl) }}
          </p>
          <p class="stat-sub" :class="totalPnl > 0 ? 'num-pos' : totalPnl < 0 ? 'num-neg' : ''">
            整体 {{ formatWeight(totalReturn) }}
          </p>
        </div>
      </div>

      <p v-if="rebalance.length" class="alert-warn">
        再平衡：{{ rebalance.map(rebalanceLabel).join('、') }}（仅提示，不下单）
      </p>

      <p v-if="positions.length === 0" class="muted">暂无持仓</p>
      <form class="book-form" @submit.prevent="saveTargets">
        <div class="book-wrap">
          <table class="book">
            <thead>
              <tr>
                <th>名称</th>
                <th>论文</th>
                <th>评估</th>
                <th>股数</th>
                <th>成本单价</th>
                <th>成本总额</th>
                <th>现价</th>
                <th>收益率</th>
                <th>市值</th>
                <th>占比</th>
                <th>目标仓位</th>
                <th>偏差</th>
                <th>规则灯</th>
                <th>浮盈亏</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in positions" :key="row.name">
                <td class="name-cell" @click="prefillTrade(row.name)">
                  <strong>{{ row.name }}</strong>
                </td>
                <td>
                  <span
                    v-if="row.thesis_status === 'current' || row.thesis_status === 'stale'"
                    class="name-cell"
                    :class="thesisClass(row)"
                    @click.stop="openThesis(row)"
                  >{{ thesisLabel(row) }}</span>
                  <span v-else class="muted">无论文</span>
                </td>
                <td>
                  <span class="name-cell eval-trigger" @click.stop="startResearch(row)">评估</span>
                </td>
                <td class="num">
                  <input
                    v-if="bookForm[row.name]"
                    v-model="bookForm[row.name]!.shares"
                    inputmode="decimal"
                    aria-label="股数"
                    :class="{ 'input-dirty': rowDirty(row) }"
                  />
                </td>
                <td class="num">
                  <input
                    v-if="bookForm[row.name]"
                    v-model="bookForm[row.name]!.unit_cost"
                    inputmode="decimal"
                    aria-label="成本单价"
                    :class="{ 'input-dirty': rowDirty(row) }"
                  />
                </td>
                <td class="num muted">{{ formatNumber(row.cost) }}</td>
                <td class="num" :class="{ overvalued: row.overvalued }">
                  <strong>{{ formatNumber(row.price) }}</strong>
                  <span v-if="row.overvalued" class="badge badge-fail">高估</span>
                  <span
                    v-if="row.overvalued && (row.cheap_bound != null || row.rich_bound != null)"
                    class="muted"
                  >低估 {{ formatNumber(row.cheap_bound) }} / 高估 {{ formatNumber(row.rich_bound) }}</span>
                  <span v-if="row.stale" class="badge badge-warn">过期</span>
                  <span v-if="isManualQuote(row.source)" class="muted">手工</span>
                  <span
                    v-if="!row.code"
                    class="name-cell badge badge-warn"
                    @click.stop="prefillCode(row.name)"
                  >无代码</span>
                </td>
                <td
                  class="num"
                  :class="(row.pnl ?? 0) > 0 ? 'num-pos' : (row.pnl ?? 0) < 0 ? 'num-neg' : ''"
                >
                  {{ formatWeight(row.price != null && row.cost ? ((row.price * row.shares) - row.cost) / row.cost : null) }}
                </td>
                <td class="num"><strong>{{ formatNumber(row.market_value) }}</strong></td>
                <td class="num">{{ formatWeight(row.weight) }}</td>
                <td class="num">
                  <input
                    v-model="targetForm[row.name]"
                    inputmode="decimal"
                    placeholder="无目标"
                    aria-label="目标仓位"
                  />
                </td>
                <td
                  class="num"
                  :class="row.deviation != null && Math.abs(row.deviation) >= 0.03 ? 'text-warn' : 'muted'"
                >
                  <span
                    v-if="row.no_target && row.dynamic_sizing"
                    class="badge"
                    :class="['drift_freeze', 'overvalued', 'exit'].includes(row.dynamic_sizing.zone)
                      ? 'badge-fail'
                      : row.dynamic_sizing.zone === 'gray_hold'
                        ? 'badge-warn'
                        : ['aggressive', 'steady'].includes(row.dynamic_sizing.zone)
                          ? 'badge-pass'
                          : 'badge-neutral'"
                  >{{ row.dynamic_sizing.label }}</span>
                  <template v-else>{{ row.no_target ? '—' : formatWeight(row.deviation) }}</template>
                </td>
                <td>
                  <span :class="ruleLightClass(row.rule_light)">{{ ruleLightLabel(row.rule_light) }}</span>
                </td>
                <td
                  class="num"
                  :class="(row.pnl ?? 0) > 0 ? 'num-pos' : (row.pnl ?? 0) < 0 ? 'num-neg' : ''"
                >
                  {{ (row.pnl ?? 0) > 0 ? '+' : '' }}{{ formatNumber(row.pnl) }}
                </td>
              </tr>
              <tr class="cash-row">
                <td>现金</td>
                <td>—</td>
                <td>—</td>
                <td class="num">—</td>
                <td class="num">—</td>
                <td class="num">—</td>
                <td class="num">—</td>
                <td class="num">—</td>
                <td class="num">
                  <input
                    v-model="cashForm"
                    inputmode="decimal"
                    aria-label="现金余额"
                    :class="{ 'input-dirty': cashDirty() }"
                  />
                </td>
                <td class="num">{{ formatWeight(cashWeight) }}</td>
                <td class="num">
                  <input
                    v-model="targetForm.__CASH__"
                    inputmode="decimal"
                    placeholder="无目标"
                    aria-label="现金目标"
                  />
                </td>
                <td class="num">{{ cashNoTarget ? '—' : formatWeight(cashDeviation) }}</td>
                <td>—</td>
                <td class="num">—</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="book-actions-bar">
          <div class="book-btns">
            <button type="button" class="btn-primary" :disabled="savingBook" @click="saveBook">
              {{ savingBook ? '保存中…' : '保存账面' }}
            </button>
            <button type="submit" :disabled="savingTargets">
              {{ savingTargets ? '保存中…' : '保存目标' }}
            </button>
          </div>
          <p class="hint">
            股数、成本单价、现金可直接改，只改账面不记买卖。分红除权也可手记一笔。收益率是现价相对成本单价。现价要改用刷新或手工价。评估在单独一列。账面改完点保存账面，目标改完点保存目标。目标填写 0 到 1 的小数，例如 0.20；空着表示无目标（现金留空即不设现金目标底线，个股目标合计不超过 1 即可）。保存为整表替换。
          </p>
        </div>
        <p v-if="bookError" class="alert-error">{{ bookError }}</p>
        <p v-if="targetError" class="alert-error">{{ targetError }}</p>
      </form>

      <section
        v-if="optimization.reduce.length || optimization.add.length || optimization.build.length"
        class="panel opt-section"
      >
        <div class="toolbar">
          <div>
            <h2>持仓优化建议</h2>
            <p class="muted sub-desc">
              基于角色上限（核心仓 10%~15% / 卫星仓 5%~8%）、三档价格带（激进 / 稳健 / 保守）、最新财报漂移与市场情绪自动生成。
            </p>
            <p v-if="optimization.sentiment_regime?.note" class="muted sub-desc">
              {{ optimization.sentiment_regime.note }}
            </p>
          </div>
          <div class="opt-summary-badges">
            <span
              v-if="optimization.sentiment_regime"
              class="badge"
              :class="optimization.sentiment_regime.zone === 'greed'
                ? 'badge-fail'
                : optimization.sentiment_regime.zone === 'fear'
                  ? 'badge-pass'
                  : 'badge-neutral'"
            >
              情绪：{{ optimization.sentiment_regime.label }}（{{ optimization.sentiment_regime.score }}分）
            </span>
            <span class="badge badge-fail">需减仓 / 风控 {{ optimization.reduce.length }}</span>
            <span class="badge badge-info">已持仓加仓 {{ optimization.add.length }}</span>
            <span class="badge badge-pass">新标的建仓 {{ optimization.build.length }}</span>
            <span class="badge badge-neutral">机动现金 {{ formatWeight(cashWeight) }}</span>
          </div>
        </div>

        <div class="opt-block" v-if="optimization.reduce.length">
          <h3>一、需要减仓 / 风控收缩（已持仓）</h3>
          <table class="opt-table">
            <thead>
              <tr>
                <th class="col-nowrap">名称 / 角色</th>
                <th class="col-nowrap">当前 → 建议区间</th>
                <th class="col-nowrap">状态预警</th>
                <th>依据与触发价</th>
                <th class="col-nowrap num">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in optimization.reduce" :key="'red-' + item.name">
                <td class="col-nowrap">
                  <strong>{{ item.name }}</strong>
                  <span class="muted">（{{ item.role_zh }}）</span>
                </td>
                <td class="col-nowrap">
                  <strong>{{ formatWeight(item.current_weight) }}</strong> → {{ item.target_range }}
                </td>
                <td class="col-nowrap">
                  <span :class="optBadgeClass(item.severity)">{{ item.tag }}</span>
                </td>
                <td class="reason-cell">{{ item.reason }}</td>
                <td class="col-nowrap num">
                  <span class="name-cell eval-trigger" @click="prefillTrade(item.name, 'sell', item.price, item.code)">记卖出</span>
                  <span
                    v-if="item.research_id"
                    class="name-cell eval-trigger"
                    style="margin-left: 0.45rem"
                    @click="router.push(`/research/${item.research_id}`)"
                  >论文</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="opt-block" v-if="optimization.add.length">
          <h3>二、可以加仓 / 待回调加仓（已持仓）</h3>
          <table class="opt-table">
            <thead>
              <tr>
                <th class="col-nowrap">名称 / 角色</th>
                <th class="col-nowrap">当前 → 建议区间</th>
                <th class="col-nowrap">买点状态</th>
                <th>依据与买点区间</th>
                <th class="col-nowrap num">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in optimization.add" :key="'add-' + item.name">
                <td class="col-nowrap">
                  <strong>{{ item.name }}</strong>
                  <span class="muted">（{{ item.role_zh }}）</span>
                </td>
                <td class="col-nowrap">
                  <strong>{{ formatWeight(item.current_weight) }}</strong> → {{ item.target_range }}
                </td>
                <td class="col-nowrap">
                  <span :class="optBadgeClass(item.severity)">{{ item.tag }}</span>
                </td>
                <td class="reason-cell">{{ item.reason }}</td>
                <td class="col-nowrap num">
                  <span class="name-cell eval-trigger" @click="prefillTrade(item.name, 'buy', item.price, item.code)">记买入</span>
                  <span
                    v-if="item.research_id"
                    class="name-cell eval-trigger"
                    style="margin-left: 0.45rem"
                    @click="router.push(`/research/${item.research_id}`)"
                  >论文</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="opt-block" v-if="optimization.build.length">
          <h3>三、可以建仓 / 新标的候选（已通过立论且财报无恶化）</h3>
          <table class="opt-table">
            <thead>
              <tr>
                <th class="col-nowrap">名称 / 角色</th>
                <th class="col-nowrap">建议建仓区间</th>
                <th class="col-nowrap">买点状态</th>
                <th>依据与三档价格带</th>
                <th class="col-nowrap num">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in optimization.build" :key="'bld-' + item.name">
                <td class="col-nowrap">
                  <strong>{{ item.name }}</strong>
                  <span class="muted">（{{ item.role_zh }}）</span>
                </td>
                <td class="col-nowrap">
                  <strong>{{ item.target_range }}</strong>
                </td>
                <td class="col-nowrap">
                  <span :class="optBadgeClass(item.severity)">{{ item.tag }}</span>
                </td>
                <td class="reason-cell">{{ item.reason }}</td>
                <td class="col-nowrap num">
                  <span class="name-cell eval-trigger" @click="prefillTrade(item.name, 'buy', item.price, item.code)">记建仓</span>
                  <span
                    v-if="item.research_id"
                    class="name-cell eval-trigger"
                    style="margin-left: 0.45rem"
                    @click="router.push(`/research/${item.research_id}`)"
                  >论文</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <div class="fold-grid">
        <details class="fold" :open="tradeOpen">
          <summary>记账（买 / 卖）</summary>
          <form class="inline-form" @submit.prevent="submitTrade">
            <label>
              日期
              <input v-model="form.date" type="date" required />
            </label>
            <label>
              方向
              <select v-model="form.direction">
                <option value="buy">买</option>
                <option value="sell">卖</option>
              </select>
            </label>
            <label class="combo">
              名称
              <input
                v-model="form.name"
                autocomplete="off"
                placeholder="输入检索或直填"
                required
                @focus="onTradeNameInput"
                @input="onTradeNameInput"
                @blur="closeTradeSuggest"
              />
              <ul v-if="tradeSuggestOpen && tradeSuggestions.length" class="suggest" role="listbox">
                <li
                  v-for="item in tradeSuggestions"
                  :key="item.code + item.name"
                  role="option"
                  @mousedown.prevent="pickTradeSuggest(item)"
                >
                  <strong>{{ item.name }}</strong>
                  <span class="muted">（{{ item.code }}）</span>
                </li>
              </ul>
            </label>
            <label>
              股数
              <input v-model="form.shares" required />
            </label>
            <label>
              价格
              <input v-model="form.price" required />
            </label>
            <label v-if="form.direction === 'buy'">
              目标仓位（可选）
              <input v-model="tradeTarget" inputmode="decimal" placeholder="如 0.10" />
            </label>
            <label>
              说明
              <input v-model="form.note" />
            </label>
            <button type="submit" class="btn-primary" :disabled="submitting">记账</button>
          </form>
          <p v-if="formError" class="alert-error">{{ formError }}</p>
          <p v-if="warning" class="alert-warn">{{ warning }}</p>
        </details>

        <details class="fold" :open="codeOpen">
          <summary>补代码 / 手工价</summary>
          <div class="dual-forms">
            <form class="inline-form" @submit.prevent="submitCode">
              <label>
                名称
                <input v-model="codeForm.name" required />
              </label>
              <label>
                代码
                <input v-model="codeForm.code" placeholder="如 600036.SH" required />
              </label>
              <button type="submit" :disabled="savingCode">补代码</button>
            </form>
            <form class="inline-form" @submit.prevent="submitManual">
              <label>
                名称
                <input v-model="manualForm.name" required />
              </label>
              <label>
                手工价
                <input v-model="manualForm.price" required />
              </label>
              <button type="submit" :disabled="savingManual">保存手工价</button>
            </form>
          </div>
        </details>

        <details class="fold">
          <summary>规则（{{ rules.length }}）</summary>
          <form class="inline-form" @submit.prevent="saveRule">
            <label>
              名称
              <input v-model="ruleForm.name" required />
            </label>
            <label>
              类型
              <select v-model="ruleForm.type">
                <option value="加仓上限价">加仓上限价</option>
              </select>
            </label>
            <label>
              价格
              <input v-model="ruleForm.price" required inputmode="decimal" />
            </label>
            <label>
              说明
              <input v-model="ruleForm.note" />
            </label>
            <button type="submit" :disabled="savingRule">保存规则</button>
          </form>
          <p v-if="ruleError" class="alert-error">{{ ruleError }}</p>
          <p v-else-if="rules.length === 0" class="muted">暂无规则</p>
          <table v-else class="rules-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th class="num">价格</th>
                <th>说明</th>
                <th class="num">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="rule in rules" :key="rule.id">
                <td><strong>{{ rule.name }}</strong></td>
                <td>{{ rule.type }}</td>
                <td class="num">{{ rule.price }}</td>
                <td class="muted">{{ rule.note || '' }}</td>
                <td class="num">
                  <button
                    type="button"
                    class="btn-sm"
                    :disabled="deletingRule != null"
                    @click="deleteRule(rule.id)"
                  >
                    删除
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </details>
      </div>

      <section v-if="review" class="panel review-section">
        <div class="toolbar">
          <h2>最近审视</h2>
          <span
            class="badge"
            :class="review.status === 'failed' ? 'badge-fail' : review.status === 'done' ? 'badge-pass' : 'badge-warn'"
          >
            状态：{{ REVIEW_STATUS[review.status] || review.status }}
          </span>
          <span v-if="review.has_stale" class="badge badge-warn">本轮含过期价</span>
          <span v-if="review.positions?.some((row) => row.short_exam)" class="badge badge-neutral">
            短体检，非六关
          </span>
        </div>
        <p v-if="review.status === 'failed'" class="alert-error">{{ review.error || '审视失败' }}</p>
        <table v-if="review.positions?.length" class="review-table">
          <thead>
            <tr>
              <th class="col-nowrap">名称</th>
              <th class="col-nowrap">动作</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in review.positions" :key="'rv-' + row.name">
              <td class="col-nowrap"><strong>{{ row.name }}</strong></td>
              <td class="col-nowrap">
                <span :class="actionBadgeClass(row.action)">{{ actionLabel(row.action) }}</span>
              </td>
              <td class="reason-cell">{{ row.reason }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="review.portfolio_actions?.length" class="portfolio-advice">
          <h3>组合建议</h3>
          <ul>
            <li v-for="(item, index) in review.portfolio_actions" :key="index">
              <strong>{{ portfolioActionLabel(item.action) }}</strong>（{{ item.names.join('、') }}）：{{ item.cash_or_target_link }}
            </li>
          </ul>
        </div>
        <details v-if="review.markdown" class="fold memo-fold">
          <summary>审视备忘录</summary>
          <article class="memo md-body" v-html="renderMarkdownHtml(review.markdown)"></article>
        </details>
      </section>
    </template>
  </section>
</template>

<style scoped>
.book th:nth-child(n + 4):not(:nth-child(13)) {
  text-align: right;
}

.input-dirty {
  border-color: #8a4b08 !important;
  background: #fdf3e6 !important;
}

.eval-trigger {
  font-size: 0.82rem;
}

.book-actions-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem 1.25rem;
  width: 100%;
  background: #fff;
  border: 1px solid #d8d2c4;
  padding: 0.6rem 0.9rem;
}

.book-btns {
  display: flex;
  gap: 0.5rem;
  flex-shrink: 0;
}

.book-actions-bar .hint {
  margin: 0;
  flex: 1 1 24rem;
  font-size: 0.82rem;
  line-height: 1.45;
}

.dual-forms {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem 2rem;
  padding-top: 0.25rem;
}

.rules-table {
  margin-top: 0.65rem;
}

.review-section,
.opt-section {
  margin-top: 1rem;
}

.opt-summary-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.opt-block {
  margin-top: 0.85rem;
}

.opt-block h3 {
  font-size: 0.94rem;
  margin: 0 0 0.4rem;
  color: #2c2925;
}

.opt-table td,
.review-table td {
  vertical-align: top;
}

.reason-cell {
  line-height: 1.6;
  color: #2c2925;
}

.portfolio-advice {
  margin-top: 0.85rem;
  padding: 0.65rem 0.9rem;
  background: #faf8f3;
  border: 1px solid #e4ddd0;
}

.portfolio-advice h3 {
  font-size: 0.95rem;
  margin: 0 0 0.35rem;
}

.portfolio-advice ul {
  margin: 0;
  padding-left: 1.2rem;
}

.memo-fold {
  margin-top: 0.75rem;
}
</style>
