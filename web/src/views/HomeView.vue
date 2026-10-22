<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import SentimentPanel from '../components/SentimentPanel.vue'
import {
  discoveryStatusText,
  jobStatusText,
  WORKBENCH_FETCH_TIMEOUT_MS,
  WORKBENCH_PATHS,
  WORKBENCH_REFRESH_MS,
} from './homeViewModel'

interface LockState {
  type?: string
  job_id?: number
}

interface JobRow {
  id: number
  type: string
  status: string
  error?: string | null
  name?: string | null
  updated_at?: string | null
}

interface CurrentDiscovery {
  id: number
  week_key: string
  status: string
}

interface WatchItem {
  id: number
  name: string
  code: string
  research_id: number
  thesis_status?: 'current' | 'stale' | 'none' | null
  thesis_research_id?: number | null
  thesis_verdict?: 'pass' | 'fail' | 'gray' | null
  current_price?: number | null
  cheap_bound?: number | null
  rich_bound?: number | null
  overvalued?: boolean
  held?: boolean
  financial_drift?: {
    period: string
    net_income_yoy?: number | null
  } | null
  dynamic_sizing?: {
    zone: string
    label: string
  } | null
}

interface ReviewSummary {
  id: number
  status: string
  error?: string | null
  has_stale?: boolean
  positions?: { name: string; action: string; short_exam?: boolean }[]
}

interface SentimentState {
  score: number | null
  zone?: string
  label: string
}

interface AftermathItem {
  research_id: number
  name: string
  as_of?: string | null
  then_verdict?: 'pass' | 'fail' | 'gray' | null
  then_price?: number | null
  current_price?: number | null
  paper?: 'current' | 'updated' | 'gone' | null
  in_band?: boolean | null
  live_verdict?: 'pass' | 'fail' | 'gray' | null
  financial_drift?: {
    period: string
    net_income_yoy?: number | null
  } | null
}

const LOCK_ZH: Record<string, string> = {
  funnel: '漏斗',
  research: '评估',
  review: '审视',
  discovery: '每周发现',
}
const PAPER_ZH: Record<string, string> = {
  current: '现行',
  updated: '已更新',
  gone: '已无',
}
const VERDICT_ZH: Record<string, string> = {
  pass: '通过',
  fail: '不通过',
  gray: '灰色地带',
}
const LIVE_ZH: Record<string, string> = {
  pass: '现价仍像通过',
  fail: '现价不像通过',
  gray: '现价仍灰色',
}

const router = useRouter()
const lock = ref<LockState | null>(null)
const jobs = ref<JobRow[]>([])
const watchlist = ref<WatchItem[]>([])
const latestReview = ref<ReviewSummary | null>(null)
const aftermath = ref<AftermathItem[]>([])
const sentiment = ref<SentimentState | null>(null)
const currentDiscovery = ref<CurrentDiscovery | null>(null)
const errorText = ref('')
const removing = ref<number | null>(null)
const researchingId = ref<number | null>(null)

const ACTION_ZH: Record<string, string> = {
  hold: '持有',
  add: '加仓',
  cut: '减仓',
  sell: '卖出',
  unknown: '无法判断',
}

const recentJobs = computed(() => jobs.value.slice(0, 8))
const sortedWatchlist = computed(() =>
  [...watchlist.value].sort((a, b) => Number(Boolean(a.held)) - Number(Boolean(b.held))),
)
const heldWatchItems = computed(() => watchlist.value.filter((item) => item.held))
const showAllAftermath = ref(false)
// /api/aftermath is newest first, so the first row per name is the latest research.
const latestAftermath = computed(() => {
  const seen = new Set<string>()
  return aftermath.value.filter((item) => !seen.has(item.name) && seen.add(item.name))
})
const shownAftermath = computed(() =>
  showAllAftermath.value ? aftermath.value : latestAftermath.value,
)
let pollTimer: number | undefined
let mounted = false
let requestSequence = 0
let workbenchController: AbortController | null = null

function jobLink(job: JobRow): string | null {
  if (job.type === 'research') return `/research/${job.id}`
  if (job.type === 'funnel') return '/screen'
  if (job.type === 'review') return '/positions'
  if (job.type === 'discovery') return '/discover'
  return null
}

function thesisLabel(item: WatchItem): string {
  const verdict = item.thesis_verdict
  let text = '论文'
  if (verdict === 'pass') text = '通过'
  else if (verdict === 'fail') text = '不通过'
  else if (verdict === 'gray') text = '灰色地带'
  if (item.thesis_status === 'stale') return `${text}·过期`
  return text
}

function thesisClass(item: WatchItem): string {
  if (item.thesis_verdict === 'fail') return 'badge badge-fail thesis-fail'
  if (item.thesis_verdict === 'pass') return 'badge badge-pass thesis-pass'
  if (item.thesis_verdict === 'gray') return 'badge badge-warn'
  return 'badge badge-neutral'
}

function watchBandLabel(item: WatchItem): { text: string; cls: string } | null {
  const p = item.current_price
  const cheap = item.cheap_bound
  const rich = item.rich_bound
  if (p == null) return null
  if (cheap != null && p <= cheap) {
    return { text: '击球区', cls: 'badge badge-pass' }
  }
  if (item.overvalued || (rich != null && p > rich)) {
    return { text: '高于上沿', cls: 'badge badge-fail' }
  }
  if (rich != null && p <= rich) {
    return { text: '带内', cls: 'badge badge-neutral' }
  }
  return null
}

function watchBandRange(item: WatchItem): string {
  if (item.cheap_bound == null && item.rich_bound == null) return '无价格带'
  if (item.cheap_bound != null && item.rich_bound != null) {
    return item.cheap_bound === item.rich_bound
      ? `上沿 ${fmtNum(item.rich_bound)}`
      : `${fmtNum(item.cheap_bound)} ~ ${fmtNum(item.rich_bound)}`
  }
  if (item.cheap_bound != null) return `买点 ≤ ${fmtNum(item.cheap_bound)}`
  return `上沿 ${fmtNum(item.rich_bound)}`
}

function fmtNum(value?: number | null): string {
  return value == null ? '—' : Number(value).toFixed(2)
}

function priceDeltaText(thenPrice?: number | null, currentPrice?: number | null): string {
  if (thenPrice == null || currentPrice == null || thenPrice <= 0) return ''
  const pct = ((currentPrice - thenPrice) / thenPrice) * 100
  const sign = pct >= 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
}

function verdictText(value?: string | null): string {
  return (value && VERDICT_ZH[value]) || value || '—'
}

function paperText(value?: string | null): string {
  return (value && PAPER_ZH[value]) || '—'
}

function bandText(value?: boolean | null): string {
  if (value == null) return '—'
  return value ? '在带内' : '带外'
}

function liveText(value?: string | null): string {
  return (value && LIVE_ZH[value]) || '无法对照'
}

function verdictClass(value?: string | null): string {
  if (value === 'fail') return 'badge badge-fail thesis-fail'
  if (value === 'pass') return 'badge badge-pass thesis-pass'
  if (value === 'gray') return 'badge badge-warn'
  return 'badge badge-neutral'
}

function actionBadgeClass(action: string): string {
  if (action === 'add') return 'badge badge-pass'
  if (action === 'cut') return 'badge badge-warn'
  if (action === 'sell') return 'badge badge-fail'
  return 'badge badge-neutral'
}

function lockLabel(): string {
  if (!lock.value) return '空闲'
  const kind = LOCK_ZH[lock.value.type || ''] || lock.value.type || '未知'
  return `占用中（${kind}）`
}

function jobDate(value?: string | null): string {
  if (!value) return '—'
  return value.slice(0, 10)
}

async function loadWorkbench() {
  if (workbenchController !== null) return
  const sequence = ++requestSequence
  const controller = new AbortController()
  const timeoutSignal = AbortSignal.timeout(WORKBENCH_FETCH_TIMEOUT_MS)
  const signal = AbortSignal.any([controller.signal, timeoutSignal])
  workbenchController = controller
  errorText.value = ''
  const messages: string[] = []
  try {
    const responses = await Promise.all(
      WORKBENCH_PATHS.map((path) => fetch(path, { signal })),
    )
    const [
      healthRes,
      jobsRes,
      watchRes,
      reviewRes,
      sentRes,
      afterRes,
      discoveryRes,
    ] = responses as [
      Response,
      Response,
      Response,
      Response,
      Response,
      Response,
      Response,
    ]
    const [
      health,
      jobsBody,
      watchBody,
      reviewBody,
      sentBody,
      afterBody,
      discoveryBody,
    ] = await Promise.all([
      healthRes.json().catch(() => ({})),
      jobsRes.json().catch(() => ({})),
      watchRes.json().catch(() => ({})),
      reviewRes.json().catch(() => ({})),
      sentRes.json().catch(() => ({})),
      afterRes.json().catch(() => ({})),
      discoveryRes.json().catch(() => ({})),
    ])
    if (!mounted || sequence !== requestSequence) return
    if (signal.aborted) {
      if (timeoutSignal.aborted) errorText.value = '工作台刷新超时'
      return
    }
    if (reviewRes.ok) {
      latestReview.value = reviewBody
    } else if (reviewRes.status === 404) {
      latestReview.value = null
    } else {
      messages.push(
        (reviewBody as { detail?: string }).detail
          || (reviewBody as { error?: string }).error
          || '无法读取审视',
      )
    }
    if (healthRes.ok) {
      lock.value = (health.lock as LockState | null) ?? null
    } else {
      messages.push(health.detail || health.error || '无法读取锁状态')
    }
    if (jobsRes.ok) {
      jobs.value = jobsBody.jobs ?? []
    } else {
      messages.push(jobsBody.detail || jobsBody.error || '无法读取任务')
    }
    if (watchRes.ok) {
      watchlist.value = watchBody.items ?? []
    } else {
      messages.push(watchBody.detail || watchBody.error || '无法读取观察池')
    }
    if (sentRes.ok) {
      sentiment.value = sentBody
    } else {
      sentiment.value = { score: null, label: '情绪不可用' }
    }
    if (afterRes.ok) {
      aftermath.value = afterBody.items ?? []
    } else {
      messages.push(
        (afterBody as { detail?: string }).detail
          || (afterBody as { error?: string }).error
          || '无法读取事后簿',
      )
    }
    if (discoveryRes.ok) {
      currentDiscovery.value = discoveryBody
    } else if (discoveryRes.status === 404) {
      currentDiscovery.value = null
    } else {
      messages.push(discoveryBody.detail || discoveryBody.error || '无法读取本周发现')
    }
    errorText.value = messages[0] || ''
  } catch {
    if (!mounted || sequence !== requestSequence) return
    errorText.value = timeoutSignal.aborted ? '工作台刷新超时' : '无法加载工作台'
  } finally {
    if (workbenchController === controller) workbenchController = null
  }
}

async function startWatchResearch(item: WatchItem) {
  if (researchingId.value != null || removing.value != null) return
  errorText.value = ''
  researchingId.value = item.id
  try {
    const response = await fetch('/api/research', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: item.name,
        code: item.code,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '重新评估失败'
      return
    }
    if (data.id == null) {
      errorText.value = '重新评估失败'
      return
    }
    await router.push(`/research/${data.id}`)
  } catch {
    errorText.value = '重新评估失败'
  } finally {
    researchingId.value = null
  }
}

async function removeWatch(id: number) {
  if (removing.value != null) return
  errorText.value = ''
  removing.value = id
  try {
    const response = await fetch(`/api/watchlist/${id}`, { method: 'DELETE' })
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '删除观察池失败'
      return
    }
    watchlist.value = watchlist.value.filter((item) => item.id !== id)
    await loadWorkbench()
  } catch {
    errorText.value = '删除观察池失败'
  } finally {
    removing.value = null
  }
}

async function clearHeldWatchlist() {
  if (removing.value != null || !heldWatchItems.value.length) return
  errorText.value = ''
  removing.value = -1
  try {
    for (const item of heldWatchItems.value) {
      await fetch(`/api/watchlist/${item.id}`, { method: 'DELETE' })
    }
    await loadWorkbench()
  } catch {
    errorText.value = '清理已持仓失败'
  } finally {
    removing.value = null
  }
}

onMounted(() => {
  mounted = true
  void loadWorkbench()
  pollTimer = window.setInterval(() => {
    void loadWorkbench()
  }, WORKBENCH_REFRESH_MS)
})

onBeforeUnmount(() => {
  mounted = false
  requestSequence += 1
  workbenchController?.abort()
  workbenchController = null
  if (pollTimer != null) window.clearInterval(pollTimer)
})
</script>

<template>
  <section class="home-page">
    <div class="toolbar">
      <h1>工作台</h1>
      <span class="badge" :class="lock ? 'badge-warn' : 'badge-pass'">
        任务锁：{{ lockLabel() }}
      </span>
    </div>
    <p v-if="errorText" class="alert-error">{{ errorText }}</p>
    <SentimentPanel :sentiment="sentiment" />

    <div class="entries">
      <article>
        <h2><RouterLink to="/positions">看仓</RouterLink></h2>
        <p>持仓、记账与审视。</p>
        <div class="entry-foot">
          <template v-if="latestReview">
            最近审视：{{ jobStatusText('review', latestReview.status) }}
          </template>
          <span v-else class="muted">进入持仓管理 →</span>
        </div>
      </article>
      <article>
        <h2><RouterLink to="/screen">选股</RouterLink></h2>
        <p>漏斗、评估与观察池。</p>
        <div class="entry-foot">
          观察池待办 <strong>{{ watchlist.length }}</strong> 只
        </div>
      </article>
      <article>
        <h2><RouterLink to="/discover">每周发现</RouterLink></h2>
        <p>全市场按周运行固定尺子，逐层确认候选。</p>
        <div class="entry-foot">
          <template v-if="currentDiscovery">
            本周批次 {{ currentDiscovery.week_key }} ·
            {{ discoveryStatusText(currentDiscovery.status) }} ·
            <RouterLink to="/discover">查看进度</RouterLink>
          </template>
          <span v-else class="muted">本周批次：尚未创建</span>
        </div>
      </article>
      <article>
        <h2><RouterLink to="/index-funds">指数基金</RouterLink></h2>
        <p>代表性指数的低估值筛选，每个指数一只代表基金。</p>
        <div class="entry-foot">
          <RouterLink to="/index-funds">查看低估指数 →</RouterLink>
        </div>
      </article>
    </div>

    <div class="home-grid">
      <section class="panel">
        <div class="toolbar">
          <h2>观察池待办</h2>
          <span class="badge badge-neutral">{{ watchlist.length }} 只</span>
          <button
            v-if="heldWatchItems.length > 0"
            type="button"
            class="btn-sm"
            :disabled="removing != null"
            @click="clearHeldWatchlist"
          >
            清理已持仓（{{ heldWatchItems.length }}）
          </button>
        </div>
        <p v-if="watchlist.length === 0" class="muted">暂无观察池待办</p>
        <table v-else>
          <thead>
            <tr>
              <th>名称 / 代码</th>
              <th class="num">现价 / 买点区间</th>
              <th>论文</th>
              <th class="num">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in sortedWatchlist" :key="item.id" :class="{ 'watch-held': item.held }">
              <td>
                <div class="watch-name-row">
                  <strong>{{ item.name }}</strong>
                  <span v-if="item.held" class="badge badge-info">已持仓</span>
                </div>
                <div class="muted watch-code">{{ item.code }}</div>
              </td>
              <td class="num">
                <div class="watch-price-row">
                  <strong>{{ fmtNum(item.current_price) }}</strong>
                  <span v-if="watchBandLabel(item)" :class="watchBandLabel(item)!.cls">
                    {{ watchBandLabel(item)!.text }}
                  </span>
                </div>
                <div class="muted watch-band">{{ watchBandRange(item) }}</div>
              </td>
              <td>
                <RouterLink
                  v-if="(item.thesis_status === 'current' || item.thesis_status === 'stale') && item.thesis_research_id != null"
                  :class="thesisClass(item)"
                  :to="`/research/${item.thesis_research_id}`"
                >{{ thesisLabel(item) }}</RouterLink>
                <span v-else class="badge badge-neutral">无论文</span>
                <span
                  v-if="item.dynamic_sizing && item.dynamic_sizing.zone === 'drift_freeze'"
                  class="badge badge-fail"
                  style="margin-left: 4px"
                >{{ item.dynamic_sizing.label }}</span>
              </td>
              <td class="num action-cell">
                <button
                  type="button"
                  class="btn-sm"
                  :disabled="researchingId != null || removing != null"
                  @click="startWatchResearch(item)"
                >
                  {{ researchingId === item.id ? '评估中…' : '重新评估' }}
                </button>
                <RouterLink class="btn-link btn-sm" :to="{ path: '/positions', query: { name: item.name } }">预填记账</RouterLink>
                <button type="button" class="btn-sm" :disabled="removing != null || researchingId != null" @click="removeWatch(item.id)">
                  删除
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel review-panel">
        <div class="toolbar">
          <h2>最近审视</h2>
          <span v-if="latestReview" class="badge" :class="latestReview.status === 'failed' ? 'badge-fail' : 'badge-pass'">
            {{ jobStatusText('review', latestReview.status) }}
          </span>
        </div>
        <p v-if="!latestReview" class="muted">暂无审视</p>
        <template v-else>
          <p v-if="latestReview.has_stale" class="alert-warn">本轮含过期价</p>
          <p v-if="latestReview.status === 'failed'" class="alert-error">{{ latestReview.error || '审视失败' }}</p>
          <div v-if="latestReview.positions?.length" class="review-pills">
            <span
              v-for="row in latestReview.positions"
              :key="row.name"
              class="review-chip"
            >
              <strong>{{ row.name }}</strong>
              <span :class="actionBadgeClass(row.action)">
                {{ ACTION_ZH[row.action] || row.action }}
              </span>
            </span>
          </div>
          <p v-else class="muted">本次审视已完成，未附带持仓动作明细。</p>
          <p v-if="latestReview.positions?.some((row) => row.short_exam)" class="muted">短体检，非六关</p>
          <div class="review-foot">
            <RouterLink class="btn-link btn-sm" to="/positions">查看持仓页审视 →</RouterLink>
          </div>
        </template>
      </section>
    </div>

    <section class="panel">
      <div class="toolbar">
        <div>
          <h2>事后簿</h2>
          <p class="muted sub-desc">对照是把当时带子套在现价上，不是收益回测。</p>
        </div>
        <label v-if="aftermath.length > latestAftermath.length" class="check-inline muted">
          <input v-model="showAllAftermath" type="checkbox" />
          显示全部历史研究（另有 {{ aftermath.length - latestAftermath.length }} 条旧研究）
        </label>
      </div>
      <p v-if="aftermath.length === 0" class="muted">暂无论文可对照</p>
      <div v-else class="book-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>当时日期</th>
              <th>当时结论</th>
              <th class="num">当时价</th>
              <th class="num">现价 / 涨跌</th>
              <th>论文状态</th>
              <th>价在带内</th>
              <th>现价对照</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in shownAftermath"
              :key="item.research_id"
              :class="{ 'row-flipped': item.then_verdict === 'pass' && (item.live_verdict === 'fail' || item.financial_drift != null) }"
            >
              <td>
                <RouterLink :to="`/research/${item.research_id}`">
                  <strong>{{ item.name }}</strong>
                </RouterLink>
              </td>
              <td class="muted col-nowrap">{{ item.as_of || '—' }}</td>
              <td>
                <span :class="verdictClass(item.then_verdict)">{{ verdictText(item.then_verdict) }}</span>
              </td>
              <td class="num">{{ fmtNum(item.then_price) }}</td>
              <td class="num">
                <strong>{{ fmtNum(item.current_price) }}</strong>
                <span
                  v-if="priceDeltaText(item.then_price, item.current_price)"
                  class="delta-tag"
                  :class="(item.current_price ?? 0) >= (item.then_price ?? 0) ? 'num-pos' : 'num-neg'"
                >
                  {{ priceDeltaText(item.then_price, item.current_price) }}
                </span>
              </td>
              <td>
                <span class="badge badge-neutral">{{ paperText(item.paper) }}</span>
              </td>
              <td>
                <span
                  v-if="item.in_band != null"
                  class="badge"
                  :class="item.in_band ? 'badge-pass' : 'badge-warn'"
                >
                  {{ bandText(item.in_band) }}
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td>
                <span :class="verdictClass(item.live_verdict)">{{ liveText(item.live_verdict) }}</span>
                <span
                  v-if="item.financial_drift"
                  class="badge badge-fail"
                  style="margin-left: 4px"
                >财报恶化（{{ item.financial_drift.period }}）</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <details class="fold">
      <summary>最近任务（{{ jobs.length }}）</summary>
      <p v-if="jobs.length === 0" class="muted">暂无任务</p>
      <table v-else>
        <thead>
          <tr>
            <th>类型</th>
            <th>状态</th>
            <th>名称</th>
            <th>日期</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="job in recentJobs" :key="`${job.type}-${job.id}`">
            <td class="col-nowrap">
              <span class="badge badge-neutral">{{ LOCK_ZH[job.type] || job.type }}</span>
            </td>
            <td class="col-nowrap">
              <span
                class="badge"
                :class="job.status === 'failed' ? 'badge-fail' : job.status === 'done' ? 'badge-pass' : 'badge-warn'"
              >
                {{ jobStatusText(job.type, job.status) }}
              </span>
            </td>
            <td>
              <RouterLink v-if="jobLink(job)" :to="jobLink(job)!">
                <strong>{{ job.name || LOCK_ZH[job.type] || job.type }}</strong>
              </RouterLink>
              <span v-else>{{ job.name || '—' }}</span>
            </td>
            <td class="muted col-nowrap">{{ jobDate(job.updated_at) }}</td>
            <td class="text-warn">{{ job.error || '' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="jobs.length > 8" class="muted">共 {{ jobs.length }} 条，仅列最近 8 条</p>
    </details>
  </section>
</template>

<style scoped>
.home-grid {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: 0.75rem;
  align-items: stretch;
}

.home-grid > .panel {
  margin: 0;
  display: flex;
  flex-direction: column;
}

.action-cell {
  display: flex;
  justify-content: flex-end;
  gap: 0.3rem;
}

.watch-name-row {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.watch-code,
.watch-band {
  font-size: 0.76rem;
  font-variant-numeric: tabular-nums;
}

.watch-price-row {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}

.watch-held td {
  opacity: 0.72;
}

.review-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin: 0.25rem 0 0.75rem;
}

.review-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.25rem 0.55rem;
  background: #faf8f3;
  border: 1px solid #e4ddd0;
  border-radius: 2px;
  font-size: 0.88rem;
}

.review-foot {
  margin-top: auto;
  padding-top: 0.5rem;
}

.sub-desc {
  margin: 0.15rem 0 0;
}

.delta-tag {
  font-size: 0.76rem;
  margin-left: 0.35rem;
}

.row-flipped td {
  background: #fdf6f6 !important;
}

@media (max-width: 820px) {
  .home-grid {
    grid-template-columns: 1fr;
  }
}
</style>
