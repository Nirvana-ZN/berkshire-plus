<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import SentimentPanel from '../components/SentimentPanel.vue'

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
}

const LOCK_ZH: Record<string, string> = {
  funnel: '漏斗',
  research: '评估',
  review: '审视',
  discovery: '每周发现',
  cheap: '低估值',
}
const JOB_STATUS: Record<string, string> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}
const DISCOVERY_STATUS: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  partially_completed: '部分完成',
  failed: '失败',
  abandoned: '已放弃',
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

const lock = ref<LockState | null>(null)
const jobs = ref<JobRow[]>([])
const watchlist = ref<WatchItem[]>([])
const latestReview = ref<ReviewSummary | null>(null)
const aftermath = ref<AftermathItem[]>([])
const sentiment = ref<SentimentState | null>(null)
const currentDiscovery = ref<CurrentDiscovery | null>(null)
const errorText = ref('')
const removing = ref<number | null>(null)

const ACTION_ZH: Record<string, string> = {
  hold: '持有',
  add: '加仓',
  cut: '减仓',
  sell: '卖出',
  unknown: '无法判断',
}

const recentJobs = computed(() => jobs.value.slice(0, 8))
let pollTimer: number | undefined

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
  if (item.thesis_verdict === 'fail') return 'thesis-fail'
  if (item.thesis_verdict === 'pass') return 'thesis-pass'
  return ''
}

function fmtNum(value?: number | null): string {
  return value == null ? '—' : String(value)
}

function verdictText(value?: string | null): string {
  return (value && VERDICT_ZH[value]) || value || '—'
}

function paperText(value?: string | null): string {
  return (value && PAPER_ZH[value]) || '—'
}

function bandText(value?: boolean | null): string {
  if (value == null) return '—'
  return value ? '在' : '不在'
}

function liveText(value?: string | null): string {
  return (value && LIVE_ZH[value]) || '无法对照'
}

function verdictClass(value?: string | null): string {
  if (value === 'fail') return 'thesis-fail'
  if (value === 'pass') return 'thesis-pass'
  return ''
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
  errorText.value = ''
  const messages: string[] = []
  try {
    const [
      healthRes,
      jobsRes,
      watchRes,
      reviewRes,
      sentRes,
      afterRes,
      discoveryRes,
    ] = await Promise.all([
      fetch('/api/health'),
      fetch('/api/jobs'),
      fetch('/api/watchlist'),
      fetch('/api/reviews/latest'),
      fetch('/api/sentiment'),
      fetch('/api/aftermath'),
      fetch('/api/discoveries/current'),
    ])
    const health = await healthRes.json()
    const jobsBody = await jobsRes.json()
    const watchBody = await watchRes.json()
    const discoveryBody = await discoveryRes.json()
    if (reviewRes.ok) {
      latestReview.value = await reviewRes.json()
    } else if (reviewRes.status === 404) {
      latestReview.value = null
    } else {
      const reviewBody = await reviewRes.json().catch(() => ({}))
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
      sentiment.value = await sentRes.json()
    } else {
      sentiment.value = { score: null, label: '情绪不可用' }
    }
    if (afterRes.ok) {
      const afterBody = await afterRes.json()
      aftermath.value = afterBody.items ?? []
    } else {
      const afterBody = await afterRes.json().catch(() => ({}))
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
    errorText.value = '无法加载工作台'
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

onMounted(() => {
  void loadWorkbench()
  pollTimer = window.setInterval(() => {
    void loadWorkbench()
  }, 3000)
})

onBeforeUnmount(() => {
  if (pollTimer != null) window.clearInterval(pollTimer)
})
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>工作台</h1>
      <p class="muted">任务锁：{{ lockLabel() }}</p>
    </div>
    <p v-if="errorText">{{ errorText }}</p>
    <SentimentPanel :sentiment="sentiment" />
    <div class="entries">
      <article>
        <h2><RouterLink to="/positions">看仓</RouterLink></h2>
        <p>持仓、记账与审视。</p>
      </article>
      <article>
        <h2><RouterLink to="/screen">选股</RouterLink></h2>
        <p>漏斗、评估与观察池。</p>
      </article>
      <article>
        <h2><RouterLink to="/discover">每周发现</RouterLink></h2>
        <p>按周运行固定尺子，逐层确认候选。</p>
        <p v-if="currentDiscovery">
          本周批次 {{ currentDiscovery.week_key }} ·
          {{ DISCOVERY_STATUS[currentDiscovery.status] || currentDiscovery.status }} ·
          <RouterLink to="/discover">查看进度</RouterLink>
        </p>
        <p v-else class="muted">本周批次：尚未创建</p>
      </article>
      <article>
        <h2><RouterLink to="/cheap">低估值（旧版）</RouterLink></h2>
        <p>沪深 A 股截面便宜榜，点进 Checklist。</p>
      </article>
    </div>

    <section class="panel">
      <h2>观察池待办</h2>
      <p v-if="watchlist.length === 0">暂无观察池待办</p>
      <table v-else>
      <thead>
          <tr>
            <th>名称</th>
            <th>代码</th>
            <th>论文</th>
            <th>操作</th>
          </tr>
      </thead>
      <tbody>
        <tr v-for="item in watchlist" :key="item.id">
          <td>{{ item.name }}</td>
          <td>{{ item.code }}</td>
          <td>
            <RouterLink
              v-if="(item.thesis_status === 'current' || item.thesis_status === 'stale') && item.thesis_research_id != null"
              :class="thesisClass(item)"
              :to="`/research/${item.thesis_research_id}`"
            >{{ thesisLabel(item) }}</RouterLink>
            <span v-else>无论文</span>
          </td>
          <td>
            <RouterLink :to="{ path: '/positions', query: { name: item.name } }">预填记账</RouterLink>
            <button type="button" :disabled="removing != null" @click="removeWatch(item.id)">
              删除
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    </section>

    <section class="panel">
    <h2>最近审视</h2>
    <p v-if="!latestReview">暂无审视</p>
    <template v-else>
      <p>
        {{ JOB_STATUS[latestReview.status] || latestReview.status }}
        <span v-if="latestReview.has_stale">　本轮含过期价</span>
      </p>
      <p v-if="latestReview.status === 'failed'">{{ latestReview.error || '审视失败' }}</p>
      <p v-if="latestReview.positions?.length">
        {{
          latestReview.positions
            .map((row) => `${row.name} ${ACTION_ZH[row.action] || row.action}`)
            .join('、')
        }}
      </p>
      <p v-if="latestReview.positions?.some((row) => row.short_exam)">短体检，非六关</p>
      <p><RouterLink to="/positions">查看持仓页审视</RouterLink></p>
    </template>
    </section>

    <section class="panel">
      <h2>事后簿</h2>
      <p class="muted">对照是把当时带子套在现价上，不是收益回测。</p>
      <p v-if="aftermath.length === 0">暂无论文可对照</p>
      <table v-else>
        <thead>
          <tr>
            <th>名称</th>
            <th>当时</th>
            <th>结论</th>
            <th>当时价</th>
            <th>现价</th>
            <th>论文</th>
            <th>价在带内</th>
            <th>对照</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in aftermath" :key="item.research_id">
            <td>
              <RouterLink :to="`/research/${item.research_id}`">{{ item.name }}</RouterLink>
            </td>
            <td>{{ item.as_of || '—' }}</td>
            <td :class="verdictClass(item.then_verdict)">{{ verdictText(item.then_verdict) }}</td>
            <td>{{ fmtNum(item.then_price) }}</td>
            <td>{{ fmtNum(item.current_price) }}</td>
            <td>{{ paperText(item.paper) }}</td>
            <td>{{ bandText(item.in_band) }}</td>
            <td :class="verdictClass(item.live_verdict)">{{ liveText(item.live_verdict) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <details class="fold">
      <summary>最近任务</summary>
      <p v-if="jobs.length === 0">暂无任务</p>
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
            <td>{{ LOCK_ZH[job.type] || job.type }}</td>
            <td>{{ JOB_STATUS[job.status] || job.status }}</td>
            <td>
              <RouterLink v-if="jobLink(job)" :to="jobLink(job)!">{{ job.name || LOCK_ZH[job.type] || job.type }}</RouterLink>
              <span v-else>{{ job.name || '—' }}</span>
            </td>
            <td>{{ jobDate(job.updated_at) }}</td>
            <td>{{ job.error || '' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="jobs.length > 8" class="muted">共 {{ jobs.length }} 条，仅列最近 8 条</p>
    </details>
  </section>
</template>
