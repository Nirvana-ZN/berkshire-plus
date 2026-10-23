<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { renderMarkdownHtml } from './homeViewModel'
import { formatPercent } from './trackingViewModel'

interface Gate {
  name: string
  status: string
  reason: string
}

interface HistoryItem {
  id: number
  date: string
  verdict: string | null
  is_thesis: boolean
}

const VERDICT_ZH: Record<string, string> = {
  pass: '通过',
  fail: '不通过',
  gray: '灰色地带',
}
const GATE_ZH: Record<string, string> = {
  pass: '通过',
  fail: '失败',
  gray: '灰色',
}
const BAND_ZH: Record<string, string> = {
  aggressive: '激进',
  steady: '稳健',
  conservative: '保守',
  watch: '观望',
}
// Band direction is undefined in the prompt (some reports put 激进 highest), so labels stay neutral.
const BAND_PRICE_ZH: Record<string, string> = {
  aggressive: '激进价',
  steady: '稳健价',
  conservative: '保守价',
  overvalued: '高估线',
}
const ROLE_ZH: Record<string, string> = {
  core: '核心',
  satellite: '卫星',
  skip: '不买',
}
const JOB_STATUS: Record<string, string> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}
const THESIS_ESTABLISHED = '已立为论文'
const THESIS_UPDATED = '已更新论文'
const VALUATION_ZH: Record<string, string> = {
  pe_ttm: '滚动市盈率（TTM）',
  pe_ttm_basis: '滚动市盈率口径',
  eps_ttm: '滚动每股收益（TTM）',
  pe_deducted_ttm: '扣非滚动市盈率（TTM）',
  pe_deducted_ttm_basis: '扣非滚动市盈率口径',
  eps_deducted_ttm: '扣非滚动每股收益（TTM）',
  pe: '静态市盈率',
  pe_basis: '静态市盈率口径',
  pb: '市净率',
  pb_tangible: '扣除商誉有形市净率',
  goodwill_pct: '商誉占净资产比（%）',
  net_cash_yi: '账面净现金（亿元，负为净负债）',
  bps: '每股净资产',
  eps: '年报每股收益',
  eps_period: '每股收益报告期',
  eps_mid: '中周期每股收益',
  pe_mid: '中周期对照市盈率',
  pe_mid_basis: '中周期市盈率口径',
  npl_pct: '不良率',
  price: '现价',
  period: '报告期',
}
const TRACK_ZH: Record<string, string> = {
  quality_discount: '优质折价',
  deep_value: '深度价值',
  garp: '合理价成长',
}

const route = useRoute()
const router = useRouter()
const errorText = ref('')
const status = ref('')
const name = ref('')
const verdict = ref('')
const markdown = ref('')
const gates = ref<Gate[]>([])
const mirror = ref<string[]>([])
const history = ref<HistoryItem[]>([])
const bandMode = ref('')
const bandPrices = ref<Record<string, string>>({})
const memoRole = ref('')
const memoEssence = ref('')
const memoRisks = ref('')
const shouldEnter = ref('')
const funding = ref('')
const overlap = ref('')
const dataGrade = ref('')
const legacyEvidenceContract = ref(false)
const structured = ref<Record<string, unknown>>({})
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function text(value: unknown): string {
  if (value == null || value === '') return '不可用'
  if (Array.isArray(value)) return value.map(text).join('；')
  if (typeof value === 'object') return Object.entries(record(value)).map(([key, item]) => `${key}：${text(item)}`).join('；')
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}
const contract = computed(() => record(structured.value.evidence_contract))
const scenarios = computed(() => record(structured.value.valuation_scenarios))
const prices = computed(() => record(scenarios.value.computed_prices))
const context = computed(() => record(structured.value.research_context))
const sourceEvidence = computed(() => Array.isArray(context.value.evidence) ? context.value.evidence.map(record) : [])
const gateAudits = computed(() => Array.isArray(structured.value.gates) ? structured.value.gates.map(record) : [])
const sensitivity = computed(() => Array.isArray(scenarios.value.sensitivity) ? scenarios.value.sensitivity.map(record) : [])
const scenarioLabels: Record<string, string> = {low: '积极买入价', base: '常规买入价', high: '停止加仓价'}
const factLabels: Record<string, string> = {metric_basis: '指标口径', period: '报告期', numerator: '指标总额（元）',
  current_shares: '当前总股数（股）', basis_id: '财务口径版本', numerator_evidence_ids: '指标证据',
  shares_evidence_id: '股数证据', profile_evidence_id: '行业证据', price_evidence_id: '价格证据'}
const assumptionLabels: Record<string, string> = {metric_per_share: '每股指标（元）', growth: '增长率（小数）',
  multiples: '情景倍数', haircuts: '情景折扣（小数）', horizon_years: '假设期限（年）', evidence_ids: '证据编号'}
function sourceUrl(value: unknown): string | undefined {
  return typeof value === 'string' && /^https?:\/\//i.test(value) ? value : undefined
}
const asOfDate = ref('')
const asOfPrice = ref<string | number | null>(null)
const valuation = ref<Record<string, string | number>>({})
const thesisMark = ref('')
const watching = ref(false)
const joined = ref(false)
const watchMessage = ref('')
const discoveryRunId = ref<number | null>(null)
const discoveryTrack = ref('')
let timer: number | undefined

const title = computed(() => name.value || '评估')
const parentPath = ref<string | null>(null)

function captureParent() {
  if (parentPath.value) return
  const back = window.history.state?.back
  if (typeof back === 'string' && back && !back.startsWith('/research')) {
    parentPath.value = back
  }
}

function goBack() {
  if (parentPath.value) {
    void router.push(parentPath.value)
    return
  }
  if (discoveryRunId.value != null) {
    void router.push('/discover')
    return
  }
  void router.push('/')
}

function fmtValue(value: string | number): string {
  const number = typeof value === 'number' ? value : Number(value)
  return String(value).trim() !== '' && Number.isFinite(number) && !Number.isInteger(number)
    ? number.toFixed(2)
    : String(value)
}

function gateBadgeClass(gateStatus: string): string {
  if (gateStatus === 'pass') return 'badge badge-pass'
  if (gateStatus === 'fail') return 'badge badge-fail'
  return 'badge badge-warn'
}

const canWatch = computed(
  () =>
    !joined.value &&
    status.value === 'done' &&
    (verdict.value === 'pass' || verdict.value === 'gray'),
)
const prefillTo = computed(() => {
  const query: Record<string, string> = {}
  if (name.value) query.name = name.value
  if (typeof asOfPrice.value === 'number' || typeof asOfPrice.value === 'string') {
    const price = String(asOfPrice.value).trim()
    if (price) query.price = price
  }
  return { path: '/positions', query }
})

function stopPoll() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

function applyResearch(data: Record<string, unknown>) {
  structured.value = record(data.structured)
  status.value = typeof data.status === 'string' ? data.status : ''
  name.value = typeof data.name === 'string' ? data.name : ''
  verdict.value = typeof data.verdict === 'string' ? data.verdict : ''
  markdown.value = typeof data.markdown === 'string' ? data.markdown : ''
  gates.value = Array.isArray(data.gates) ? (data.gates as Gate[]) : []
  mirror.value = Array.isArray(data.mirror_test) ? (data.mirror_test as string[]) : []
  history.value = Array.isArray(data.history) ? (data.history as HistoryItem[]) : []
  discoveryRunId.value = typeof data.discovery_run_id === 'number'
    ? data.discovery_run_id
    : null
  discoveryTrack.value = typeof data.discovery_track === 'string'
    ? data.discovery_track
    : ''
  const bands = data.price_bands
  bandMode.value =
    bands && typeof bands === 'object' && 'mode' in bands
      ? String((bands as { mode?: string }).mode || '')
      : ''
  bandPrices.value = {}
  if (bands && typeof bands === 'object') {
    for (const key of Object.keys(BAND_PRICE_ZH)) {
      const text = (bands as Record<string, unknown>)[key]
      if (typeof text === 'string' && text.trim()) bandPrices.value[key] = text.trim()
    }
  }
  const memo = data.memo
  if (memo && typeof memo === 'object') {
    const row = memo as Record<string, unknown>
    memoRole.value = typeof row.role === 'string' ? row.role : ''
    memoEssence.value = typeof row.essence === 'string' ? row.essence : ''
    memoRisks.value = typeof row.risks === 'string' ? row.risks : ''
  }
  const fit = data.portfolio_fit
  if (fit && typeof fit === 'object') {
    const row = fit as Record<string, unknown>
    shouldEnter.value = typeof row.should_enter === 'string' ? row.should_enter : ''
    funding.value = typeof row.funding === 'string' ? row.funding : ''
    overlap.value = typeof row.overlap === 'string' ? row.overlap : ''
  }
  dataGrade.value = typeof data.data_grade === 'string' ? data.data_grade : ''
  legacyEvidenceContract.value = data.legacy_evidence_contract === true
  const asOf = data.as_of
  if (asOf && typeof asOf === 'object') {
    const row = asOf as Record<string, unknown>
    asOfDate.value = typeof row.date === 'string' ? row.date : ''
    asOfPrice.value = (row.price as string | number | null) ?? null
  }
  const snap = data.valuation
  valuation.value = {}
  if (snap && typeof snap === 'object') {
    const rawSnap = snap as Record<string, unknown>
    const orderedKeys = [
      ...Object.keys(VALUATION_ZH).filter((k) => k in rawSnap),
      ...Object.keys(rawSnap).filter((k) => !(k in VALUATION_ZH)),
    ]
    for (const key of orderedKeys) {
      const value = rawSnap[key]
      if (value !== null && value !== undefined && value !== '') {
        valuation.value[key] = value as string | number
      }
    }
  }
  if (data.thesis_mark === THESIS_UPDATED) {
    thesisMark.value = THESIS_UPDATED
  } else if (data.thesis_mark === THESIS_ESTABLISHED) {
    thesisMark.value = THESIS_ESTABLISHED
  } else {
    thesisMark.value = ''
  }
  if (data.status === 'failed') {
    errorText.value = typeof data.error === 'string' ? data.error : '评估失败'
  }
}

async function loadResearch() {
  const id = route.params.id
  try {
    const response = await fetch(`/api/research/${id}`)
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '无法读取评估'
      status.value = 'failed'
      stopPoll()
      return
    }
    applyResearch(data)
    if (data.status === 'done' || data.status === 'failed') {
      stopPoll()
    }
  } catch {
    errorText.value = '无法读取评估'
    stopPoll()
  }
}

async function addToWatchlist() {
  if (!canWatch.value || watching.value) return
  watchMessage.value = ''
  watching.value = true
  try {
    const response = await fetch('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ research_id: Number(route.params.id) }),
    })
    const data = await response.json()
    if (!response.ok) {
      watchMessage.value = data.detail || data.error || '无法加入观察池'
      return
    }
    watchMessage.value = '已加入观察池'
    joined.value = true
  } catch {
    watchMessage.value = '无法加入观察池'
  } finally {
    watching.value = false
  }
}

onMounted(async () => {
  captureParent()
  await loadResearch()
  if (status.value !== 'done' && status.value !== 'failed') {
    timer = window.setInterval(() => {
      void loadResearch()
    }, 800)
  }
})

watch(
  () => route.params.id,
  async () => {
    stopPoll()
    await loadResearch()
    if (status.value !== 'done' && status.value !== 'failed') {
      timer = window.setInterval(() => {
        void loadResearch()
      }, 800)
    }
  },
)

onBeforeUnmount(stopPoll)
</script>

<template>
  <section class="research-page">
    <div class="toolbar">
      <button type="button" class="btn-link back-link" @click="goBack">← 返回</button>
      <div class="title-group">
        <h1>{{ title }}</h1>
        <span
          v-if="status"
          class="badge"
          :class="status === 'failed' ? 'badge-fail' : status === 'done' ? 'badge-pass' : 'badge-warn'"
        >
          状态：{{ JOB_STATUS[status] || status }}
        </span>
        <span
          v-if="thesisMark"
          class="badge"
          :class="{ 'badge-pass thesis-pass': verdict === 'pass', 'badge-fail thesis-fail': verdict === 'fail', 'badge-warn': verdict === 'gray' }"
        >
          {{ thesisMark }}
        </span>
        <span v-if="discoveryRunId !== null && discoveryTrack" class="source-badge">
          来源：每周发现 / {{ TRACK_ZH[discoveryTrack] || discoveryTrack }}
        </span>
      </div>
      <div v-if="status === 'done'" class="action-group">
        <label v-if="history.length > 1" class="history-select">
          <span class="muted">历史评估</span>
          <select
            :value="Number(route.params.id)"
            @change="router.push(`/research/${($event.target as HTMLSelectElement).value}`)"
          >
            <option v-for="item in history" :key="item.id" :value="item.id">
              #{{ item.id }} · {{ item.date || '未知日期' }} · {{ (item.verdict && VERDICT_ZH[item.verdict]) || item.verdict || '—' }}{{ item.is_thesis ? '（现行论文）' : '' }}
            </option>
          </select>
        </label>
        <button v-if="canWatch" type="button" class="btn-primary" :disabled="watching" @click="addToWatchlist">
          加入观察池
        </button>
        <RouterLink v-if="name" class="btn-link" :to="prefillTo">预填记账</RouterLink>
      </div>
    </div>

    <p v-if="errorText" class="alert-error">{{ errorText }}</p>
    <p v-if="watchMessage" class="alert-ok">{{ watchMessage }}</p>

    <template v-if="status === 'done'">
      <section class="panel conclusion-panel">
        <h2>结论条</h2>
        <div class="stat-grid conclusion-strip">
          <div class="stat-card">
            <p class="stat-label">评估结论</p>
            <p
              class="stat-value"
              :class="{ 'thesis-pass': verdict === 'pass', 'thesis-fail': verdict === 'fail', 'thesis-gray': verdict === 'gray' }"
            >
              {{ VERDICT_ZH[verdict] || verdict }}
            </p>
            <p class="stat-sub">仓位角色：{{ ROLE_ZH[memoRole] || memoRole || '—' }}</p>
          </div>
          <div class="stat-card">
            <p class="stat-label">所用股价</p>
            <p class="stat-value">{{ asOfPrice == null ? '无现价' : fmtValue(asOfPrice) }}</p>
            <p class="stat-sub">研究日期：{{ asOfDate || '—' }}</p>
          </div>
          <div class="stat-card">
            <p class="stat-label">价格带模式</p>
            <p class="stat-value">{{ BAND_ZH[bandMode] || bandMode || '—' }}</p>
            <p class="stat-sub">信息丰富度：{{ dataGrade || '—' }}</p>
            <p v-if="legacyEvidenceContract" class="stat-sub">旧版研究（legacy_evidence_contract）：原文与原等级保留，未按冻结证据合同核验。</p>
          </div>
        </div>

        <div v-if="!legacyEvidenceContract && Object.keys(prices).length" class="band-grid">
          <div v-for="(label, key) in scenarioLabels" :key="key" class="band-item">
            <span class="band-label">{{ label }}</span><strong class="band-val">{{ text(prices[key]) }} 元</strong>
          </div>
        </div>
        <div v-else-if="Object.keys(bandPrices).length" class="band-grid">
          <div
            v-for="(text, key) in bandPrices"
            :key="'b-' + key"
            class="band-item"
            :class="key === 'overvalued' ? 'band-over' : ''"
          >
            <span class="band-label">{{ BAND_PRICE_ZH[key] }}</span>
            <strong class="band-val">{{ text }}</strong>
          </div>
        </div>

        <div v-if="Object.keys(valuation).length" class="val-grid">
          <div v-for="(value, key) in valuation" :key="'v-' + key" class="val-cell">
            <span class="val-key">{{ VALUATION_ZH[key] || key }}</span>
            <strong class="val-num">{{ fmtValue(value) }}</strong>
          </div>
        </div>
      </section>

      <section v-if="!legacyEvidenceContract && Object.keys(contract).length" class="panel evidence-audit">
        <h2>冻结证据与缺口</h2>
        <p>A 表示证据覆盖，不保证结论正确或尽职调查完整。</p>
        <p>证据截止：{{ text(contract.cutoff_at) }} · 估值版本：{{ text(contract.assumption_id) }}</p>
        <p>财务证据完整：{{ text(contract.financial_complete) }} · 关键论断完整：{{ text(contract.critical_claims_complete) }} · 估值完整：{{ text(contract.valuation_complete) }}</p>
        <p>估值缺口：{{ text(contract.valuation_reason) }} · 论断缺口：{{ text(contract.errors) }}</p>
        <div v-for="(audit, index) in gateAudits" :key="index">
          <h3>{{ text(audit.name) }}</h3>
          <p>证据：{{ text(audit.evidence_ids) }} · 反证：{{ text(audit.counterevidence_ids) }}</p>
          <p>反证检索范围：{{ text(record(audit.counterevidence_search).scope) }}</p>
          <p>失效条件：{{ text(audit.falsification_condition) }} · 核验时间：{{ text(audit.checked_at) }}</p>
          <p>缺口：{{ text(record(audit.claim_evidence).errors || audit.errors) }}</p>
        </div>
        <details><summary>冻结来源（{{ sourceEvidence.length }}）</summary>
          <article v-for="source in sourceEvidence" :key="text(source.evidence_id)">
            <h3>{{ text(source.kind) }} · {{ text(source.period) }}</h3>
            <p class="source-evidence-id">证据编号：{{ text(source.evidence_id) }}</p>
            <p>状态：{{ text(record(source.source_proof).status) }} · 缺口：{{ text(record(source.source_proof).reason) }}</p>
            <p><a v-if="sourceUrl(source.source_url)" :href="sourceUrl(source.source_url)" target="_blank" rel="noopener noreferrer">原始来源</a> · {{ text(record(source.source_proof).report_location) }}</p>
            <p>{{ text(record(source.source_proof).excerpt) }}</p>
            <p>文件哈希：{{ text(source.document_sha256) }}</p>
          </article>
        </details>
      </section>

      <section v-if="!legacyEvidenceContract && Object.keys(prices).length" class="panel evidence-audit">
        <h2>可复算价格情景</h2>
        <p>{{ text(scenarios.version) }} · {{ text(scenarios.method) }} · 公式：{{ text(scenarios.formula) }}</p>
        <h3>来源事实</h3>
        <p v-for="(value, key) in record(scenarios.source_facts)" :key="key">{{ factLabels[key] || key }}：{{ text(value) }}</p>
        <h3>审阅后的情景假设</h3>
        <p>增长、倍数和折扣是情景声明。</p>
        <p v-for="(value, key) in record(scenarios.declared_assumptions)" :key="key">{{ assumptionLabels[key] || key }}：{{ text(value) }}</p>
        <p>悲观情景：{{ text(record(scenarios.pessimistic).price) }} 元 · {{ text(record(scenarios.pessimistic).description) }}</p>
        <p>估值利差：{{ formatPercent(typeof record(scenarios.valuation_spread).value === 'string' ? record(scenarios.valuation_spread).value as string : null) }} · 国债日期 {{ text(record(scenarios.valuation_spread).treasury_date) }} · 来源 {{ text(record(scenarios.valuation_spread).treasury_source) }} · 状态 {{ text(record(scenarios.valuation_spread).reason || record(scenarios.valuation_spread).status) }}</p>
        <details><summary>增长 / 倍数敏感性</summary><div class="scenario-scroll"><table>
          <thead><tr><th>增长率（小数）</th><th>倍数</th><th>折扣（小数）</th><th>价格（元）</th></tr></thead>
          <tbody><tr v-for="(row, index) in sensitivity" :key="index"><td>{{ text(row.growth) }}</td><td>{{ text(row.multiple) }}</td><td>{{ text(row.haircut) }}</td><td>{{ text(row.price) }}</td></tr></tbody>
        </table></div></details>
      </section>

      <div class="memo-fit-grid">
        <section class="panel">
          <h2>备忘</h2>
          <dl class="kv-list">
            <div>
              <dt>生意本质</dt>
              <dd>{{ memoEssence || '—' }}</dd>
            </div>
            <div class="risk-box">
              <dt>主要风险</dt>
              <dd>{{ memoRisks || '—' }}</dd>
            </div>
          </dl>
        </section>

        <section class="panel">
          <h2>对照当前仓</h2>
          <dl class="kv-list">
            <div>
              <dt>该不该进当前仓</dt>
              <dd>{{ shouldEnter || '—' }}</dd>
            </div>
            <div>
              <dt>用什么钱买</dt>
              <dd>{{ funding || '—' }}</dd>
            </div>
            <div>
              <dt>是否叠逻辑</dt>
              <dd>{{ overlap || '—' }}</dd>
            </div>
          </dl>
        </section>
      </div>

      <details class="fold" open>
        <summary>六关</summary>
        <table>
          <thead>
            <tr>
              <th class="col-nowrap">关</th>
              <th class="col-nowrap">结果</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="gate in gates" :key="gate.name">
              <td class="col-nowrap"><strong>{{ gate.name }}</strong></td>
              <td class="col-nowrap">
                <span :class="gateBadgeClass(gate.status)">
                  {{ GATE_ZH[gate.status] || gate.status }}
                </span>
              </td>
              <td class="gate-reason">{{ gate.reason }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <details class="fold">
        <summary>镜子测试（{{ mirror.length }}）</summary>
        <table>
          <tbody>
            <tr v-for="(line, index) in mirror" :key="'m-' + index">
              <td class="col-nowrap muted">#{{ index + 1 }}</td>
              <td>{{ line }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <details class="fold">
        <summary>决策备忘录</summary>
        <article class="memo md-body" v-html="renderMarkdownHtml(markdown)"></article>
      </details>
    </template>
  </section>
</template>

<style scoped>
.back-link {
  flex: 0 0 auto;
  padding: 0.2rem 0.15rem;
  font-size: 0.92rem;
}

.title-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.55rem;
  margin-right: auto;
}

.title-group h1 {
  margin: 0;
}

.action-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}

.history-select {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.source-badge {
  display: inline-block;
  border: 1px solid #d8d2c4;
  background: #faf8f3;
  border-radius: 2px;
  padding: 0.12rem 0.5rem;
  font-size: 0.8rem;
  color: #464139;
}

.conclusion-strip {
  margin-bottom: 0.75rem;
}

.band-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  gap: 0.55rem;
  margin-bottom: 0.75rem;
}

.band-item {
  background: #f3efe6;
  border: 1px solid #dfd7c7;
  border-left: 3px solid #1b5e4a;
  padding: 0.45rem 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.band-item.band-over {
  border-left-color: #9f1d1d;
  background: #fdf6f6;
}

.band-label {
  font-size: 0.76rem;
  color: #5c564c;
}

.band-val {
  font-size: 0.95rem;
  font-variant-numeric: tabular-nums;
  color: #1a1a1a;
}

.val-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(11.5rem, 1fr));
  gap: 0.45rem;
  border-top: 1px dashed #e4ddd0;
  padding-top: 0.7rem;
}

.val-cell {
  background: #faf8f3;
  border: 1px solid #e8e2d5;
  padding: 0.4rem 0.65rem;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.val-key {
  font-size: 0.76rem;
  color: #5c564c;
}

.val-num {
  font-size: 0.92rem;
  font-variant-numeric: tabular-nums;
  color: #1a1a1a;
}

.memo-fit-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}

.memo-fit-grid > .panel {
  margin: 0 0 0.75rem;
}

.kv-list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
}

.kv-list > div {
  padding: 0.55rem 0.75rem;
  background: #faf8f3;
  border: 1px solid #e8e2d5;
}

.kv-list > div.risk-box {
  border-left: 3px solid #8a4b08;
  background: #fdfaf4;
}

.kv-list dt {
  font-size: 0.78rem;
  font-weight: 600;
  color: #5c564c;
  margin-bottom: 0.2rem;
}

.kv-list dd {
  margin: 0;
  line-height: 1.55;
  font-size: 0.92rem;
  color: #1a1a1a;
}

.gate-reason {
  line-height: 1.55;
}

.evidence-audit p, .evidence-audit article { overflow-wrap: anywhere; }
.source-evidence-id { min-width: 0; white-space: normal; overflow-wrap: anywhere; }
.scenario-scroll { max-width: 100%; overflow-x: auto; }

@media (max-width: 820px) {
  .memo-fit-grid {
    grid-template-columns: 1fr;
  }
}
</style>
