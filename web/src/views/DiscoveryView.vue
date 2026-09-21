<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

const DISCOVERY_RUN_STATUSES = [
  'queued',
  'running',
  'waiting_confirmation',
  'completed',
  'partially_completed',
  'failed',
  'abandoned',
] as const

type DiscoveryRunStatus = (typeof DISCOVERY_RUN_STATUSES)[number]

interface DiscoveryRun {
  id: number
  week_key: string
  status: DiscoveryRunStatus
  error?: string | null
}

type ViewState =
  | { kind: 'loading' }
  | { kind: 'empty' }
  | { kind: 'ready'; run: DiscoveryRun; source: 'loaded' | 'created' }
  | { kind: 'error'; message: string }

const state = ref<ViewState>({ kind: 'loading' })
const starting = ref(false)

const STATUS_ZH: Record<DiscoveryRunStatus, string> = {
  queued: '排队中',
  running: '进行中',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  partially_completed: '部分完成',
  failed: '失败',
  abandoned: '已放弃',
}

const announcement = computed(() => {
  if (starting.value) return '正在创建本周发现…'
  const current = state.value
  if (current.kind === 'loading') return '正在读取本周批次…'
  if (current.kind === 'empty') return '本周尚未创建批次。可以开始本周发现。'
  if (current.kind === 'error') return current.message
  const prefix = current.source === 'created' ? '本周发现创建成功。' : '本周批次已加载。'
  return `${prefix}状态：${STATUS_ZH[current.run.status]}`
})

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isDiscoveryRunStatus(value: unknown): value is DiscoveryRunStatus {
  return DISCOVERY_RUN_STATUSES.some((status) => status === value)
}

function isDiscoveryRun(value: unknown): value is DiscoveryRun {
  if (!isRecord(value)) return false
  return (
    typeof value.id === 'number'
    && Number.isInteger(value.id)
    && value.id > 0
    && typeof value.week_key === 'string'
    && value.week_key.length > 0
    && isDiscoveryRunStatus(value.status)
    && (value.error == null || typeof value.error === 'string')
  )
}

function errorMessage(body: unknown, fallback: string): string {
  if (!isRecord(body)) return fallback
  if (typeof body.detail === 'string' && body.detail) return body.detail
  if (typeof body.error === 'string' && body.error) return body.error
  return fallback
}

async function loadCurrent() {
  state.value = { kind: 'loading' }
  try {
    const response = await fetch('/api/discoveries/current')
    if (response.status === 404) {
      state.value = { kind: 'empty' }
      return
    }
    const body: unknown = await response.json()
    if (!response.ok) {
      state.value = { kind: 'error', message: errorMessage(body, '无法读取本周发现') }
    } else if (!isDiscoveryRun(body)) {
      state.value = { kind: 'error', message: '服务返回的本周批次数据无效' }
    } else {
      state.value = { kind: 'ready', run: body, source: 'loaded' }
    }
  } catch {
    state.value = { kind: 'error', message: '无法读取本周发现' }
  }
}

async function startDiscovery() {
  if (starting.value) return
  starting.value = true
  try {
    const response = await fetch('/api/discoveries', { method: 'POST' })
    const body: unknown = await response.json()
    if (!response.ok) {
      state.value = { kind: 'error', message: errorMessage(body, '无法开始本周发现') }
    } else if (!isDiscoveryRun(body)) {
      state.value = { kind: 'error', message: '服务返回的本周批次数据无效' }
    } else {
      state.value = { kind: 'ready', run: body, source: 'created' }
    }
  } catch {
    state.value = { kind: 'error', message: '无法开始本周发现' }
  } finally {
    starting.value = false
  }
}

onMounted(() => {
  void loadCurrent()
})
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>每周发现</h1>
      <p class="muted">按固定尺子筛选，再逐层确认。</p>
    </div>

    <p role="status" aria-live="polite" aria-atomic="true">{{ announcement }}</p>

    <div v-if="state.kind === 'empty'" class="panel">
      <h2>本周尚未创建批次</h2>
      <p class="muted">创建后将从共同排雷开始。</p>
      <button type="button" :disabled="starting" @click="startDiscovery">
        {{ starting ? '正在开始…' : '开始本周发现' }}
      </button>
    </div>

    <div v-else-if="state.kind === 'error'" class="panel">
      <h2>暂时无法显示</h2>
      <p role="alert">{{ state.message }}</p>
      <button type="button" @click="loadCurrent">重新加载</button>
    </div>

    <div v-else-if="state.kind === 'ready'" class="panel">
      <h2>本周批次</h2>
      <p>周次：{{ state.run.week_key }}</p>
      <p>状态：{{ STATUS_ZH[state.run.status] }}</p>
      <p v-if="state.run.error" role="alert">{{ state.run.error }}</p>
    </div>
  </section>
</template>
