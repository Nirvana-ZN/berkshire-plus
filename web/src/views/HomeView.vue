<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

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

interface WatchItem {
  id: number
  name: string
  code: string
  research_id: number
  thesis_status?: 'current' | 'stale' | 'none' | null
  thesis_research_id?: number | null
}

interface ReviewSummary {
  id: number
  status: string
  error?: string | null
  has_stale?: boolean
  positions?: { name: string; action: string; short_exam?: boolean }[]
}

const LOCK_ZH: Record<string, string> = {
  funnel: '漏斗',
  research: '评估',
  review: '审视',
}
const JOB_STATUS: Record<string, string> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}

const lock = ref<LockState | null>(null)
const jobs = ref<JobRow[]>([])
const watchlist = ref<WatchItem[]>([])
const latestReview = ref<ReviewSummary | null>(null)
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

function thesisLabel(status?: string | null): string {
  if (status === 'current') return '有效'
  if (status === 'stale') return '过期'
  return '无论文'
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
    const [healthRes, jobsRes, watchRes, reviewRes] = await Promise.all([
      fetch('/api/health'),
      fetch('/api/jobs'),
      fetch('/api/watchlist'),
      fetch('/api/reviews/latest'),
    ])
    const health = await healthRes.json()
    const jobsBody = await jobsRes.json()
    const watchBody = await watchRes.json()
    if (reviewRes.ok) {
      latestReview.value = await reviewRes.json()
    } else if (reviewRes.status === 404) {
      latestReview.value = null
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
})
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>工作台</h1>
      <p class="muted">任务锁：{{ lockLabel() }}</p>
    </div>
    <p v-if="errorText">{{ errorText }}</p>
    <div class="entries">
      <article>
        <h2><RouterLink to="/positions">看仓</RouterLink></h2>
        <p>持仓、记账与审视。</p>
      </article>
      <article>
        <h2><RouterLink to="/screen">选股</RouterLink></h2>
        <p>漏斗、评估与观察池。</p>
      </article>
    </div>

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
              :to="`/research/${item.thesis_research_id}`"
            >{{ thesisLabel(item.thesis_status) }}</RouterLink>
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
            <td>{{ job.name || '—' }}</td>
            <td>{{ jobDate(job.updated_at) }}</td>
            <td>{{ job.error || '' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="jobs.length > 8" class="muted">共 {{ jobs.length }} 条，仅列最近 8 条</p>
    </details>
  </section>
</template>
