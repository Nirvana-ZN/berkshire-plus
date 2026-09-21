<script setup lang="ts">
import { onMounted, ref } from 'vue'

interface DiscoveryRun {
  id: number
  week_key: string
  status: string
  error?: string | null
}

interface ApiError {
  detail?: string
  error?: string
}

type ViewState =
  | { kind: 'loading' }
  | { kind: 'empty' }
  | { kind: 'ready'; run: DiscoveryRun }
  | { kind: 'error'; message: string }

const state = ref<ViewState>({ kind: 'loading' })
const starting = ref(false)

const STATUS_ZH: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  waiting_confirmation: '等待确认',
  done: '已完成',
  failed: '失败',
  abandoned: '已放弃',
}

function errorMessage(body: ApiError, fallback: string): string {
  return body.detail || body.error || fallback
}

async function loadCurrent() {
  state.value = { kind: 'loading' }
  try {
    const response = await fetch('/api/discoveries/current')
    if (response.status === 404) {
      state.value = { kind: 'empty' }
      return
    }
    const body = (await response.json()) as DiscoveryRun & ApiError
    state.value = response.ok
      ? { kind: 'ready', run: body }
      : { kind: 'error', message: errorMessage(body, '无法读取本周发现') }
  } catch {
    state.value = { kind: 'error', message: '无法读取本周发现' }
  }
}

async function startDiscovery() {
  if (starting.value) return
  starting.value = true
  try {
    const response = await fetch('/api/discoveries', { method: 'POST' })
    const body = (await response.json()) as DiscoveryRun & ApiError
    state.value = response.ok
      ? { kind: 'ready', run: body }
      : { kind: 'error', message: errorMessage(body, '无法开始本周发现') }
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

    <p v-if="state.kind === 'loading'" role="status" aria-live="polite">正在读取本周批次…</p>

    <div v-else-if="state.kind === 'empty'" class="panel">
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

    <div v-else class="panel">
      <h2>本周批次</h2>
      <p>周次：{{ state.run.week_key }}</p>
      <p>状态：{{ STATUS_ZH[state.run.status] || state.run.status }}</p>
      <p v-if="state.run.error" role="alert">{{ state.run.error }}</p>
    </div>
  </section>
</template>
