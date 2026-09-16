<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

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

const route = useRoute()
const router = useRouter()
const positions = ref<PositionRow[]>([])
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
}) {
  positions.value = data.positions ?? []
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
    next[row.name] = row.no_target ? '' : String(row.target ?? '')
    book[row.name] = {
      shares: String(row.shares),
      unit_cost: formatUnitCost(row.cost, row.shares),
    }
  }
  next.__CASH__ = cashNoTarget.value ? '' : String(cashTarget.value ?? '')
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
  if (row.thesis_verdict === 'fail') return 'thesis-fail'
  if (row.thesis_verdict === 'pass') return 'thesis-pass'
  return ''
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
    applyBook(data)
    form.value = {
      date: todayIso(),
      direction: 'buy',
      name: '',
      shares: '',
      price: '',
      note: '',
    }
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

function prefillTrade(name: string) {
  form.value.name = name
  tradeOpen.value = true
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
      <button type="button" :disabled="refreshing" @click="refreshQuotes">刷新现价</button>
      <select v-model="reviewDepth" aria-label="审视深度">
        <option value="reuse">引用论文，无则短体检</option>
        <option value="eval_first">缺论文先评估再审视</option>
      </select>
      <button type="button" :disabled="reviewing" @click="startReview">一键审视</button>
    </div>
    <p v-if="loadError">{{ loadError }}</p>
    <template v-else>
      <p v-if="migrateError">{{ migrateError }}{{ migrateDetail ? `：${migrateDetail}` : '' }}</p>
      <p v-if="reviewError">{{ reviewError }}</p>
      <p v-if="researchError">{{ researchError }}</p>
      <p v-if="quoteError">{{ quoteError }}</p>
      <p v-for="item in refreshErrors" :key="item">{{ item }}</p>
      <p class="hint">股数、成本单价、现金可直接改，只改账面不记买卖。分红除权也可手记一笔。收益率是现价相对成本单价。现价要改用刷新或手工价。评估在单独一列。账面改完点保存账面，目标改完点保存目标。</p>
      <p v-if="rebalance.length">
        再平衡：{{ rebalance.map(rebalanceLabel).join('、') }}（仅提示，不下单）
      </p>
      <p v-if="positions.length === 0">暂无持仓</p>
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
                <td class="name-cell" @click="prefillTrade(row.name)">{{ row.name }}</td>
                <td>
                  <span
                    v-if="row.thesis_status === 'current' || row.thesis_status === 'stale'"
                    class="name-cell"
                    :class="thesisClass(row)"
                    @click.stop="openThesis(row)"
                  >{{ thesisLabel(row) }}</span>
                  <span v-else>无论文</span>
                </td>
                <td>
                  <span class="name-cell" @click.stop="startResearch(row)">评估</span>
                </td>
                <td>
                  <input
                    v-if="bookForm[row.name]"
                    v-model="bookForm[row.name]!.shares"
                    inputmode="decimal"
                    aria-label="股数"
                  />
                </td>
                <td>
                  <input
                    v-if="bookForm[row.name]"
                    v-model="bookForm[row.name]!.unit_cost"
                    inputmode="decimal"
                    aria-label="成本单价"
                  />
                </td>
                <td>{{ formatNumber(row.cost) }}</td>
                <td :class="{ overvalued: row.overvalued }">
                  {{ formatNumber(row.price) }}
                  <span v-if="row.overvalued">高估</span>
                  <span
                    v-if="row.overvalued && (row.cheap_bound != null || row.rich_bound != null)"
                    class="muted"
                  >低估 {{ formatNumber(row.cheap_bound) }} / 高估 {{ formatNumber(row.rich_bound) }}</span>
                  <span v-if="row.stale">过期</span>
                  <span v-if="isManualQuote(row.source)" class="muted">手工</span>
                  <span
                    v-if="!row.code"
                    class="name-cell"
                    @click.stop="prefillCode(row.name)"
                  >无代码</span>
                </td>
                <td>{{ formatWeight(row.price != null && row.cost ? ((row.price * row.shares) - row.cost) / row.cost : null) }}</td>
                <td>{{ formatNumber(row.market_value) }}</td>
                <td>{{ formatWeight(row.weight) }}</td>
                <td>
                  <input
                    v-model="targetForm[row.name]"
                    inputmode="decimal"
                    placeholder="无目标"
                    aria-label="目标仓位"
                  />
                </td>
                <td>{{ row.no_target ? '—' : formatWeight(row.deviation) }}</td>
                <td>{{ ruleLightLabel(row.rule_light) }}</td>
                <td>{{ formatNumber(row.pnl) }}</td>
              </tr>
              <tr class="cash-row">
                <td>现金</td>
                <td>—</td>
                <td>—</td>
                <td>—</td>
                <td>—</td>
                <td>—</td>
                <td>—</td>
                <td>—</td>
                <td>
                  <input v-model="cashForm" inputmode="decimal" aria-label="现金余额" />
                </td>
                <td>{{ formatWeight(cashWeight) }}</td>
                <td>
                  <input
                    v-model="targetForm.__CASH__"
                    inputmode="decimal"
                    placeholder="无目标"
                    aria-label="现金目标"
                  />
                </td>
                <td>{{ cashNoTarget ? '—' : formatWeight(cashDeviation) }}</td>
                <td>—</td>
                <td>—</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p>目标填写 0 到 1 的小数，例如 0.20；空着表示无目标。保存为整表替换。</p>
        <button type="button" :disabled="savingBook" @click="saveBook">保存账面</button>
        <button type="submit" :disabled="savingTargets">保存目标</button>
        <p v-if="bookError">{{ bookError }}</p>
        <p v-if="targetError">{{ targetError }}</p>
      </form>
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
          <label>
            名称
            <input v-model="form.name" required />
          </label>
          <label>
            股数
            <input v-model="form.shares" required />
          </label>
          <label>
            价格
            <input v-model="form.price" required />
          </label>
          <label>
            说明
            <input v-model="form.note" />
          </label>
          <button type="submit" :disabled="submitting">记账</button>
        </form>
        <p v-if="formError">{{ formError }}</p>
        <p v-if="warning">{{ warning }}</p>
      </details>
      <details class="fold" :open="codeOpen">
        <summary>补代码 / 手工价</summary>
        <form class="inline-form" @submit.prevent="submitCode">
          <label>
            名称
            <input v-model="codeForm.name" required />
          </label>
          <label>
            代码
            <input v-model="codeForm.code" required />
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
      </details>
      <details class="fold">
        <summary>规则</summary>
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
        <p v-if="ruleError">{{ ruleError }}</p>
        <p v-else-if="rules.length === 0">暂无规则</p>
        <table v-else>
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>价格</th>
              <th>说明</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="rule in rules" :key="rule.id">
              <td>{{ rule.name }}</td>
              <td>{{ rule.type }}</td>
              <td>{{ rule.price }}</td>
              <td>{{ rule.note || '' }}</td>
              <td>
                <button
                  type="button"
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
      <section v-if="review" class="fold">
        <h2>最近审视</h2>
        <p>状态：{{ REVIEW_STATUS[review.status] || review.status }}</p>
        <p v-if="review.has_stale">本轮含过期价</p>
        <p v-if="review.status === 'failed'">{{ review.error || '审视失败' }}</p>
        <p v-if="review.positions?.some((row) => row.short_exam)">短体检，非六关</p>
        <table v-if="review.positions?.length">
          <thead>
            <tr>
              <th>名称</th>
              <th>动作</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in review.positions" :key="'rv-' + row.name">
              <td>{{ row.name }}</td>
              <td>{{ actionLabel(row.action) }}</td>
              <td>{{ row.reason }}</td>
            </tr>
          </tbody>
        </table>
        <template v-if="review.portfolio_actions?.length">
          <h3>组合建议</h3>
          <ul>
            <li v-for="(item, index) in review.portfolio_actions" :key="index">
              {{ portfolioActionLabel(item.action) }}（{{ item.names.join('、') }}）：{{ item.cash_or_target_link }}
            </li>
          </ul>
        </template>
        <details v-if="review.markdown" class="fold">
          <summary>审视备忘录</summary>
          <article class="memo">{{ review.markdown }}</article>
        </details>
      </section>
    </template>
  </section>
</template>
