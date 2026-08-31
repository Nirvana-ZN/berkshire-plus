<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

interface ScreenRow {
  code: string
  name: string
  verdict: string
  reason?: string | null
  role?: string | null
  finalist?: boolean
  mcap?: number | null
  industry?: string | null
}

interface ResearchJobRow {
  id: number
  name: string
  code: string
  status: string
  error?: string | null
}

const ROLE_LABEL: Record<string, string> = {
  high_certainty: '高确定性',
  high_elasticity: '高弹性',
  mid_elasticity: '中弹性',
}

const JOB_STATUS: Record<string, string> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}

const scopeType = ref('行业')
const scopeText = ref('')
const evaluate = ref(true)
const submitting = ref(false)
const status = ref('')
const errorText = ref('')
const emptyHint = '尚未运行漏斗，此处不展示名单。'
const keep = ref<ScreenRow[]>([])
const drop = ref<ScreenRow[]>([])
const insufficient = ref<ScreenRow[]>([])
const finalists = ref<ScreenRow[]>([])
const jobs = ref<ResearchJobRow[]>([])
const activeId = ref<number | null>(null)
let timer: number | undefined

const busy = computed(() => {
  if (status.value === 'queued' || status.value === 'running') {
    return true
  }
  return jobs.value.some((row) => row.status === 'queued' || row.status === 'running')
})

function roleLabel(role?: string | null): string {
  if (!role) return ''
  return ROLE_LABEL[role] || role
}

function applyScreen(data: {
  status?: string
  error?: string | null
  keep?: ScreenRow[]
  drop?: ScreenRow[]
  insufficient?: ScreenRow[]
  finalists?: ScreenRow[]
  jobs?: ResearchJobRow[]
  scope_type?: string
  scope_text?: string
}) {
  status.value = data.status || ''
  keep.value = data.keep ?? []
  drop.value = data.drop ?? []
  insufficient.value = data.insufficient ?? []
  finalists.value = data.finalists ?? []
  jobs.value = data.jobs ?? []
  if (data.scope_type) {
    scopeType.value = data.scope_type
  }
  if (data.scope_text) {
    scopeText.value = data.scope_text
  }
  if (data.status === 'failed') {
    errorText.value = data.error || '漏斗失败'
  }
}

function stillBusy(data: { status?: string; jobs?: ResearchJobRow[] }) {
  if (data.status === 'queued' || data.status === 'running') {
    return true
  }
  return (data.jobs ?? []).some((row) => row.status === 'queued' || row.status === 'running')
}

function stopPoll() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

async function loadScreen(id: number) {
  try {
    const response = await fetch(`/api/screens/${id}`)
    const data = await response.json()
    if (activeId.value !== id) {
      return
    }
    if (!response.ok) {
      errorText.value = data.detail || data.error || '无法读取选股结果'
      stopPoll()
      return
    }
    applyScreen(data)
    if (!stillBusy(data)) {
      stopPoll()
    }
  } catch {
    if (activeId.value !== id) {
      return
    }
    errorText.value = '无法读取选股结果'
    stopPoll()
  }
}

async function loadLatest() {
  try {
    const response = await fetch('/api/screens/latest')
    if (response.status === 404) {
      return
    }
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '无法读取选股结果'
      return
    }
    const id = Number(data.id)
    if (!id) {
      return
    }
    activeId.value = id
    applyScreen(data)
    if (busy.value) {
      timer = window.setInterval(() => {
        void loadScreen(id)
      }, 800)
    }
  } catch {
    errorText.value = '无法读取选股结果'
  }
}

async function submitScope() {
  if (submitting.value || busy.value) {
    return
  }
  errorText.value = ''
  submitting.value = true
  try {
    const response = await fetch('/api/screens/funnel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scope_type: scopeType.value,
        scope_text: scopeText.value,
        evaluate: evaluate.value,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '提交失败'
      return
    }
    stopPoll()
    const id = Number(data.id)
    activeId.value = id
    status.value = data.status || 'queued'
    keep.value = []
    drop.value = []
    insufficient.value = []
    finalists.value = []
    jobs.value = []
    await loadScreen(id)
    if (busy.value) {
      timer = window.setInterval(() => {
        void loadScreen(id)
      }, 800)
    }
  } catch {
    errorText.value = '提交失败'
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  void loadLatest()
})

onBeforeUnmount(stopPoll)
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>选股</h1>
      <p v-if="status" class="muted">{{ scopeType }}　{{ scopeText }}</p>
    </div>
    <form class="inline-form" @submit.prevent="submitScope">
      <label>
        范围类型
        <select v-model="scopeType">
          <option value="行业">行业</option>
          <option value="指数">指数</option>
          <option value="主题">主题</option>
        </select>
      </label>
      <label>
        范围
        <input v-model="scopeText" type="text" name="scope_text" />
      </label>
      <label class="check-inline">
        <input v-model="evaluate" type="checkbox" />
        评估终选
      </label>
      <button type="submit" :disabled="submitting || busy">开始筛选</button>
    </form>
    <p v-if="status">状态：{{ JOB_STATUS[status] || status }}</p>
    <p v-if="errorText">{{ errorText }}</p>
    <p v-if="!status && !errorText">{{ emptyHint }}</p>

    <template v-if="jobs.length && status !== 'done'">
      <h2>评估子任务</h2>
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>状态</th>
            <th>入口</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in jobs" :key="'j-' + row.id">
            <td>{{ row.name }}</td>
            <td>{{ JOB_STATUS[row.status] || row.status }}</td>
            <td>
              <router-link :to="'/research/' + row.id">查看评估</router-link>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-if="status === 'done'">
      <h2>终选</h2>
      <p v-if="finalists.length === 0">本轮无终选</p>
      <table v-else>
        <thead>
          <tr>
            <th>名称</th>
            <th>角色</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in finalists" :key="'f-' + row.code">
            <td>{{ row.name }}</td>
            <td>{{ roleLabel(row.role) }}</td>
          </tr>
        </tbody>
      </table>

      <template v-if="jobs.length">
        <h2>评估子任务</h2>
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>入口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in jobs" :key="'jd-' + row.id">
              <td>{{ row.name }}</td>
              <td>{{ JOB_STATUS[row.status] || row.status }}</td>
              <td>
                <router-link :to="'/research/' + row.id">查看评估</router-link>
              </td>
            </tr>
          </tbody>
        </table>
      </template>

      <details class="fold">
        <summary>保留（{{ keep.length }}）</summary>
        <h2>保留</h2>
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>代码</th>
              <th>角色</th>
              <th>终选</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in keep" :key="'k-' + row.code">
              <td>{{ row.name }}</td>
              <td>{{ row.code }}</td>
              <td>{{ roleLabel(row.role) }}</td>
              <td>{{ row.finalist ? '是' : '' }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <details class="fold">
        <summary>淘汰（{{ drop.length }}）</summary>
        <h2>淘汰</h2>
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in drop" :key="'d-' + row.code">
              <td>{{ row.name }}</td>
              <td>{{ row.reason }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <details class="fold">
        <summary>数据不足（{{ insufficient.length }}）</summary>
        <h2>数据不足</h2>
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in insufficient" :key="'i-' + row.code">
              <td>{{ row.name }}</td>
              <td>{{ row.reason }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </template>
  </section>
</template>
