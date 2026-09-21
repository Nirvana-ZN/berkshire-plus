<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const DISCOVERY_RUN_STATUSES = [
  'queued',
  'running',
  'waiting_confirmation',
  'completed',
  'partially_completed',
  'failed',
  'abandoned',
] as const

const STAGE_STATUSES = [
  'queued',
  'running',
  'waiting_confirmation',
  'confirmed',
  'failed',
  'data_not_ready',
  'ruler_not_ready',
  'insufficient_candidates',
] as const

const TRACKS = ['quality_discount', 'deep_value', 'garp'] as const
const TRACK_STAGES = ['track_gate', 'competition', 'adjudication'] as const

type DiscoveryRunStatus = (typeof DISCOVERY_RUN_STATUSES)[number]
type StageStatus = (typeof STAGE_STATUSES)[number]
type Track = (typeof TRACKS)[number]
type TrackStage = (typeof TRACK_STAGES)[number]
type StageKey = 'safety' | `${Track}:${TrackStage}`
type MetricValue = string | number | null

interface RunStagePointer {
  stage: string | null
  status: StageStatus | null
  created: boolean
}

interface DiscoveryRun {
  id: number
  week_key: string
  status: DiscoveryRunStatus
  error?: string | null
  safety?: RunStagePointer
  tracks?: Partial<Record<Track, RunStagePointer>>
}

interface DiscoveryOverride {
  id: number
  track: Track
  stage: TrackStage
  attempt: number
  code: string
  action: 'forced_pass' | 'forced_drop'
  reason: string
}

interface DiscoveryRow {
  code: string
  name: string
  verdict: 'pass' | 'drop' | 'insufficient'
  reason: string | null
  rank: number | null
  metrics: Record<string, string | number | null>
  override: DiscoveryOverride | null
}

interface SummaryRule {
  id: string
  text: string
}

interface StageCounts {
  input: number
  pass: number
  drop: number
  insufficient: number
  overrides: number
}

interface MetricDistribution {
  status: 'available' | 'unavailable'
  reason?: string
  count: number
  min: string | null
  p25: string | null
  median: string | null
  p75: string | null
  max: string | null
}

interface ThresholdMarker {
  id: string
  threshold: string
  metric: string | null
  direction: 'gt' | 'gte' | 'lte' | null
  position: string | null
  status: 'available' | 'unavailable' | 'unresolved'
}

interface StageSummary {
  schema_version: string
  rules: SummaryRule[]
  ruler: {
    version_id: number | null
    reason: string
    scope: string
    algorithm_version: string | null
    status: string
  }
  counts: StageCounts
  distributions: Record<string, MetricDistribution>
  threshold_markers: ThresholdMarker[]
}

interface StageCurrent {
  attempt: number
  status: StageStatus
  error: string | null
  failure_kind: 'transient' | 'permanent' | null
}

interface StageResponse {
  current: StageCurrent
  pass: DiscoveryRow[]
  drop: DiscoveryRow[]
  insufficient: DiscoveryRow[]
  overrides: DiscoveryOverride[]
  summary: StageSummary
}

interface StageView {
  key: StageKey
  title: string
  track: Track | null
  stage: 'safety' | TrackStage
  created: boolean
  status: StageStatus
  payload: StageResponse | null
  error: string
}

type ViewState =
  | { kind: 'loading' }
  | { kind: 'empty' }
  | { kind: 'ready'; run: DiscoveryRun; source: 'loaded' | 'created' }
  | { kind: 'error'; message: string }

const STATUS_ZH: Record<DiscoveryRunStatus, string> = {
  queued: '排队中',
  running: '进行中',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  partially_completed: '部分完成',
  failed: '失败',
  abandoned: '已放弃',
}

const STAGE_STATUS_ZH: Record<StageStatus, string> = {
  queued: '排队中',
  running: '进行中',
  waiting_confirmation: '等待确认',
  confirmed: '已确认',
  failed: '失败',
  data_not_ready: '数据未就绪',
  ruler_not_ready: '尺子未就绪',
  insufficient_candidates: '候选不足',
}

const TRACK_ZH: Record<Track, string> = {
  quality_discount: '优质折价',
  deep_value: '深度价值',
  garp: '合理价成长',
}

const STAGE_ZH: Record<TrackStage, string> = {
  track_gate: '轨道门槛',
  competition: '候选竞争',
  adjudication: '人工裁决',
}

const state = ref<ViewState>({ kind: 'loading' })
const starting = ref(false)
const mutating = ref(false)
const actionError = ref('')
const stageViews = ref<StageView[]>([])
const activeRunId = ref<number | null>(null)
const activeStageKey = ref<Record<string, number>>({})
const overrideCode = ref('')
const overrideAction = ref<'forced_pass' | 'forced_drop'>('forced_pass')
const overrideReason = ref('')
let pollTimer: number | undefined

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

function isStageStatus(value: unknown): value is StageStatus {
  return STAGE_STATUSES.some((status) => status === value)
}

function isTrack(value: unknown): value is Track {
  return TRACKS.some((track) => track === value)
}

function isTrackStage(value: unknown): value is TrackStage {
  return TRACK_STAGES.some((stage) => stage === value)
}

function isRunStagePointer(value: unknown): value is RunStagePointer {
  return isRecord(value)
    && (value.stage === null || typeof value.stage === 'string')
    && (value.status === null || isStageStatus(value.status))
    && typeof value.created === 'boolean'
}

function isDiscoveryRun(value: unknown): value is DiscoveryRun {
  if (!isRecord(value)) return false
  if (
    typeof value.id !== 'number'
    || !Number.isInteger(value.id)
    || value.id <= 0
    || typeof value.week_key !== 'string'
    || !value.week_key
    || !isDiscoveryRunStatus(value.status)
    || (value.error != null && typeof value.error !== 'string')
  ) return false
  if (value.safety !== undefined && !isRunStagePointer(value.safety)) return false
  if (value.tracks !== undefined) {
    if (!isRecord(value.tracks)) return false
    for (const track of TRACKS) {
      if (value.tracks[track] !== undefined && !isRunStagePointer(value.tracks[track])) return false
    }
  }
  return true
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function nonNegativeInteger(value: unknown): number | null {
  const number = finiteNumber(value)
  return number !== null && Number.isInteger(number) && number >= 0 ? number : null
}

function textOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function metricValue(value: unknown): MetricValue | undefined {
  if (value === null || typeof value === 'string') return value
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function parseOverride(value: unknown): DiscoveryOverride | null {
  if (
    !isRecord(value)
    || nonNegativeInteger(value.id) === null
    || !isTrack(value.track)
    || !isTrackStage(value.stage)
    || nonNegativeInteger(value.attempt) === null
    || typeof value.code !== 'string'
    || (value.action !== 'forced_pass' && value.action !== 'forced_drop')
    || typeof value.reason !== 'string'
  ) return null
  return {
    id: value.id as number,
    track: value.track,
    stage: value.stage,
    attempt: value.attempt as number,
    code: value.code,
    action: value.action,
    reason: value.reason,
  }
}

function parseRow(value: unknown): DiscoveryRow | null {
  if (
    !isRecord(value)
    || typeof value.code !== 'string'
    || (value.verdict !== 'pass' && value.verdict !== 'drop' && value.verdict !== 'insufficient')
    || (value.reason !== null && typeof value.reason !== 'string')
    || (value.rank !== undefined && value.rank !== null && nonNegativeInteger(value.rank) === null)
    || !isRecord(value.metrics)
  ) return null
  const metrics: Record<string, MetricValue> = {}
  for (const [key, raw] of Object.entries(value.metrics)) {
    const parsed = metricValue(raw)
    if (parsed === undefined) return null
    metrics[key] = parsed
  }
  const override = value.override == null ? null : parseOverride(value.override)
  if (value.override != null && override === null) return null
  return {
    code: value.code,
    name: typeof value.name === 'string' && value.name ? value.name : value.code,
    verdict: value.verdict,
    reason: textOrNull(value.reason),
    rank: value.rank == null ? null : value.rank as number,
    metrics,
    override,
  }
}

function parseRows(value: unknown): DiscoveryRow[] | null {
  if (!Array.isArray(value)) return null
  const rows = value.map(parseRow)
  return rows.some((row) => row === null) ? null : rows as DiscoveryRow[]
}

function parseSummary(value: unknown): StageSummary | null {
  if (!isRecord(value) || !Array.isArray(value.rules) || !isRecord(value.ruler)
    || !isRecord(value.counts) || !isRecord(value.distributions)
    || !Array.isArray(value.threshold_markers)) return null
  const rules: SummaryRule[] = []
  for (const rule of value.rules) {
    if (!isRecord(rule) || typeof rule.id !== 'string' || typeof rule.text !== 'string') return null
    rules.push({ id: rule.id, text: rule.text })
  }
  const counts = value.counts
  const parsedCounts = {
    input: nonNegativeInteger(counts.input),
    pass: nonNegativeInteger(counts.pass),
    drop: nonNegativeInteger(counts.drop),
    insufficient: nonNegativeInteger(counts.insufficient),
    overrides: nonNegativeInteger(counts.overrides),
  }
  if (Object.values(parsedCounts).some((count) => count === null)) return null
  const distributions: Record<string, MetricDistribution> = {}
  for (const [metric, raw] of Object.entries(value.distributions)) {
    if (!isRecord(raw) || (raw.status !== 'available' && raw.status !== 'unavailable')) return null
    const count = nonNegativeInteger(raw.count)
    if (count === null) return null
    distributions[metric] = {
      status: raw.status,
      reason: typeof raw.reason === 'string' ? raw.reason : undefined,
      count,
      min: textOrNull(raw.min),
      p25: textOrNull(raw.p25),
      median: textOrNull(raw.median),
      p75: textOrNull(raw.p75),
      max: textOrNull(raw.max),
    }
  }
  const markers: ThresholdMarker[] = []
  for (const marker of value.threshold_markers) {
    if (
      !isRecord(marker)
      || typeof marker.id !== 'string'
      || typeof marker.threshold !== 'string'
      || (marker.metric !== null && typeof marker.metric !== 'string')
      || ![null, 'gt', 'gte', 'lte'].includes(marker.direction as null | string)
      || (marker.position !== null && typeof marker.position !== 'string')
      || !['available', 'unavailable', 'unresolved'].includes(String(marker.status))
    ) return null
    markers.push(marker as unknown as ThresholdMarker)
  }
  const ruler = value.ruler
  if (
    (ruler.version_id !== null && nonNegativeInteger(ruler.version_id) === null)
    || typeof ruler.reason !== 'string'
    || typeof ruler.scope !== 'string'
    || (ruler.algorithm_version !== null && typeof ruler.algorithm_version !== 'string')
    || typeof ruler.status !== 'string'
  ) return null
  return {
    schema_version: typeof value.schema_version === 'string' ? value.schema_version : 'unavailable',
    rules,
    ruler: {
      version_id: ruler.version_id as number | null,
      reason: ruler.reason,
      scope: ruler.scope,
      algorithm_version: ruler.algorithm_version as string | null,
      status: ruler.status,
    },
    counts: parsedCounts as StageCounts,
    distributions,
    threshold_markers: markers,
  }
}

function parseStageResponse(value: unknown): StageResponse | null {
  if (!isRecord(value) || !isRecord(value.current) || !isStageStatus(value.current.status)) return null
  const passed = parseRows(value.pass)
  const dropped = parseRows(value.drop)
  const insufficient = parseRows(value.insufficient)
  const summary = parseSummary(value.summary)
  if (passed === null || dropped === null || insufficient === null || summary === null) return null
  const overrides = Array.isArray(value.overrides)
    ? value.overrides.map(parseOverride)
    : []
  if (overrides.some((item) => item === null)) return null
  return {
    current: {
      attempt: nonNegativeInteger(value.current.attempt) ?? 0,
      status: value.current.status,
      error: textOrNull(value.current.error),
      failure_kind: value.current.failure_kind === 'transient' || value.current.failure_kind === 'permanent'
        ? value.current.failure_kind
        : null,
    },
    pass: passed,
    drop: dropped,
    insufficient,
    overrides: overrides as DiscoveryOverride[],
    summary,
  }
}

function errorMessage(body: unknown, fallback: string): string {
  if (!isRecord(body)) return fallback
  if (typeof body.detail === 'string' && body.detail) return body.detail
  if (typeof body.error === 'string' && body.error) return body.error
  return fallback
}

function viewsForRun(run: DiscoveryRun): StageView[] {
  const safetyStatus = run.safety?.status ?? 'queued'
  const views: StageView[] = [{
    key: 'safety',
    title: '共同排雷',
    track: null,
    stage: 'safety',
    created: run.safety?.created ?? true,
    status: safetyStatus,
    payload: null,
    error: '',
  }]
  for (const track of TRACKS) {
    const pointer = run.tracks?.[track]
    const stage = isTrackStage(pointer?.stage) ? pointer.stage : 'track_gate'
    views.push({
      key: `${track}:${stage}`,
      title: `${TRACK_ZH[track]} · ${STAGE_ZH[stage]}`,
      track,
      stage,
      created: pointer?.created ?? false,
      status: pointer?.status ?? 'queued',
      payload: null,
      error: '',
    })
  }
  return views
}

function endpoint(view: StageView): string {
  const runId = activeRunId.value
  return view.track === null
    ? `/api/discoveries/${runId}/stages/safety`
    : `/api/discoveries/${runId}/tracks/${view.track}/stages/${view.stage}`
}

function stopPoll() {
  if (pollTimer !== undefined) {
    window.clearTimeout(pollTimer)
    pollTimer = undefined
  }
}

function needsPolling(): boolean {
  return stageViews.value.some(
    (view) => view.created && (view.status === 'queued' || view.status === 'running'),
  )
}

function schedulePoll() {
  stopPoll()
  if (!needsPolling()) return
  pollTimer = window.setTimeout(() => {
    void refreshRun()
  }, 800)
}

async function loadStage(view: StageView, runId: number) {
  const requestNumber = (activeStageKey.value[view.key] ?? 0) + 1
  activeStageKey.value[view.key] = requestNumber
  try {
    const response = await fetch(endpoint(view))
    const body: unknown = await response.json()
    if (activeRunId.value !== runId || activeStageKey.value[view.key] !== requestNumber) return
    const current = stageViews.value.find((item) => item.key === view.key)
    if (!current) return
    if (response.status === 404 && (current.status === 'queued' || current.status === 'running')) return
    if (!response.ok) {
      current.error = errorMessage(body, '无法读取阶段结果')
      return
    }
    const payload = parseStageResponse(body)
    if (!payload) {
      current.error = '服务返回的阶段数据无效'
      return
    }
    current.payload = payload
    current.created = true
    current.status = payload.current.status
    current.error = ''
  } catch {
    if (activeRunId.value !== runId || activeStageKey.value[view.key] !== requestNumber) return
    const current = stageViews.value.find((item) => item.key === view.key)
    if (current) current.error = '无法读取阶段结果'
  }
}

async function applyRun(run: DiscoveryRun, source: 'loaded' | 'created') {
  stopPoll()
  activeRunId.value = run.id
  const previous = new Map(stageViews.value.map((view) => [view.key, view]))
  stageViews.value = viewsForRun(run).map((view) => {
    const old = previous.get(view.key)
    return old ? { ...view, payload: old.payload, error: old.error } : view
  })
  state.value = { kind: 'ready', run, source }
  await Promise.all(stageViews.value.map((view) => loadStage(view, run.id)))
  schedulePoll()
}

async function readCurrent(source: 'loaded' | 'created' = 'loaded'): Promise<boolean> {
  try {
    const response = await fetch('/api/discoveries/current')
    if (response.status === 404) {
      stopPoll()
      activeRunId.value = null
      stageViews.value = []
      state.value = { kind: 'empty' }
      return false
    }
    const body: unknown = await response.json()
    if (!response.ok) {
      state.value = { kind: 'error', message: errorMessage(body, '无法读取本周发现') }
    } else if (!isDiscoveryRun(body)) {
      state.value = { kind: 'error', message: '服务返回的本周批次数据无效' }
    } else {
      await applyRun(body, source)
      return true
    }
  } catch {
    state.value = { kind: 'error', message: '无法读取本周发现' }
  }
  stopPoll()
  return false
}

async function loadCurrent() {
  state.value = { kind: 'loading' }
  await readCurrent()
}

async function refreshRun() {
  const runId = activeRunId.value
  if (runId === null) return
  await readCurrent()
  if (activeRunId.value !== runId) stopPoll()
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
      await applyRun(body, 'created')
    }
  } catch {
    state.value = { kind: 'error', message: '无法开始本周发现' }
  } finally {
    starting.value = false
  }
}

async function stageMutation(view: StageView, action: 'confirm' | 'retry') {
  if (mutating.value || activeRunId.value === null) return
  actionError.value = ''
  mutating.value = true
  try {
    const response = await fetch(`${endpoint(view)}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    const body: unknown = await response.json()
    if (!response.ok) actionError.value = errorMessage(body, '阶段操作失败')
    else await refreshRun()
  } catch {
    actionError.value = '阶段操作失败'
  } finally {
    mutating.value = false
  }
}

async function saveOverride(view: StageView) {
  if (mutating.value || activeRunId.value === null || view.track === null || view.stage === 'safety') return
  const reason = overrideReason.value.trim()
  const code = overrideCode.value.trim()
  if (!code || !reason) {
    actionError.value = '代码和覆盖理由均为必填'
    return
  }
  actionError.value = ''
  mutating.value = true
  try {
    const response = await fetch(`/api/discoveries/${activeRunId.value}/overrides`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        track: view.track,
        stage: view.stage,
        attempt: view.payload?.current.attempt,
        code,
        action: overrideAction.value,
        reason,
      }),
    })
    const body: unknown = await response.json()
    if (!response.ok) {
      actionError.value = errorMessage(body, '人工覆盖保存失败')
    } else {
      overrideCode.value = ''
      overrideReason.value = ''
      await loadStage(view, activeRunId.value)
    }
  } catch {
    actionError.value = '人工覆盖保存失败'
  } finally {
    mutating.value = false
  }
}

async function abandonRun() {
  if (mutating.value || activeRunId.value === null) return
  actionError.value = ''
  mutating.value = true
  try {
    const response = await fetch(`/api/discoveries/${activeRunId.value}/abandon`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    const body: unknown = await response.json()
    if (!response.ok) actionError.value = errorMessage(body, '无法放弃本批次')
    else await refreshRun()
  } catch {
    actionError.value = '无法放弃本批次'
  } finally {
    mutating.value = false
  }
}

function rows(view: StageView): DiscoveryRow[] {
  const payload = view.payload
  return payload ? [...payload.pass, ...payload.drop, ...payload.insufficient] : []
}

function metricKeys(view: StageView): string[] {
  return [...new Set(rows(view).flatMap((row) => Object.keys(row.metrics)))].sort()
}

function verdictLabel(verdict: DiscoveryRow['verdict']): string {
  return { pass: '通过', drop: '淘汰', insufficient: '数据不足' }[verdict]
}

function formatValue(value: unknown): string {
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '不可用'
  if (typeof value === 'string' && value) return value
  return '不可用'
}

function markerPosition(marker: ThresholdMarker): string {
  if (marker.status !== 'available' || marker.position === null) return '不可用'
  const position = Number(marker.position)
  return Number.isFinite(position) ? `${position}%` : '不可用'
}

onMounted(() => {
  void loadCurrent()
})

onBeforeUnmount(() => {
  activeRunId.value = null
  activeStageKey.value = {}
  stopPoll()
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

    <template v-else-if="state.kind === 'ready'">
      <div class="panel run-summary">
        <h2>本周批次</h2>
        <p>周次：{{ state.run.week_key }}　状态：{{ STATUS_ZH[state.run.status] }}</p>
        <p v-if="state.run.error" role="alert">{{ state.run.error }}</p>
      </div>

      <p v-if="actionError" role="alert">{{ actionError }}</p>

      <article
        v-for="view in stageViews"
        :key="view.key"
        class="panel stage-panel"
        :aria-busy="view.created && (view.status === 'queued' || view.status === 'running')"
      >
        <div class="toolbar">
          <h2>{{ view.title }}</h2>
          <p class="muted">状态：{{ STAGE_STATUS_ZH[view.status] }}</p>
        </div>

        <p v-if="view.error" role="alert">{{ view.error }}</p>
        <p v-if="view.payload?.current.error" role="alert">{{ view.payload.current.error }}</p>
        <p v-if="!view.created" class="muted">
          等待前置阶段确认。
        </p>
        <p v-else-if="view.status === 'queued' || view.status === 'running'" class="muted">
          正在生成本层结果，完成后会自动停止刷新。
        </p>
        <p v-else-if="view.status === 'data_not_ready' || view.status === 'ruler_not_ready'">
          本层只读且不可重试；请放弃本批次并重新创建。
        </p>
        <p v-else-if="view.status === 'failed'" class="muted">
          仅瞬时失败可重试；永久失败请放弃后重新创建。
        </p>

        <template v-if="view.payload">
          <section class="stage-summary" aria-label="阶段摘要">
            <div>
              <h3>本层固定规则</h3>
              <ul v-if="view.payload.summary.rules.length">
                <li v-for="rule in view.payload.summary.rules" :key="rule.id">{{ rule.text }}</li>
              </ul>
              <p v-else class="muted">不可用</p>
            </div>
            <div>
              <h3>尺子版本/改因</h3>
              <p>
                {{ view.payload.summary.ruler.version_id ?? '内置/不可用' }}
                · {{ view.payload.summary.ruler.reason || '不可用' }}
              </p>
            </div>
            <div>
              <h3>计数</h3>
              <p>
                输入 {{ view.payload.summary.counts.input }} ·
                通过 {{ view.payload.summary.counts.pass }} ·
                淘汰 {{ view.payload.summary.counts.drop }} ·
                数据不足 {{ view.payload.summary.counts.insufficient }} ·
                人工覆盖 {{ view.payload.summary.counts.overrides }}
              </p>
            </div>
          </section>

          <details class="summary-details">
            <summary>指标分布与阈值位置</summary>
            <h3>指标分布</h3>
            <div class="table-wrap" tabindex="0" aria-label="指标分布，可横向滚动">
              <table>
                <thead>
                  <tr>
                    <th>指标</th><th>数量</th><th>最小</th><th>P25</th>
                    <th>中位</th><th>P75</th><th>最大</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(distribution, metric) in view.payload.summary.distributions" :key="metric">
                    <td>{{ metric }}</td>
                    <td>{{ distribution.count }}</td>
                    <template v-if="distribution.status === 'available'">
                      <td>{{ formatValue(distribution.min) }}</td>
                      <td>{{ formatValue(distribution.p25) }}</td>
                      <td>{{ formatValue(distribution.median) }}</td>
                      <td>{{ formatValue(distribution.p75) }}</td>
                      <td>{{ formatValue(distribution.max) }}</td>
                    </template>
                    <td v-else colspan="5">不可用</td>
                  </tr>
                  <tr v-if="!Object.keys(view.payload.summary.distributions).length">
                    <td colspan="7">不可用</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <h3>阈值位置</h3>
            <ul v-if="view.payload.summary.threshold_markers.length">
              <li v-for="marker in view.payload.summary.threshold_markers" :key="marker.id">
                {{ marker.id }}：{{ marker.threshold }}（{{ markerPosition(marker) }}）
              </li>
            </ul>
            <p v-else class="muted">不可用</p>
          </details>

          <div class="table-wrap" tabindex="0" :aria-label="`${view.title}结果，可横向滚动`">
            <table>
              <caption>{{ view.title }}结构化结果</caption>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>代码</th>
                  <th>判定</th>
                  <th>理由</th>
                  <th>排名</th>
                  <th v-for="metric in metricKeys(view)" :key="metric">{{ metric }}</th>
                  <th>人工覆盖</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in rows(view)" :key="row.code">
                  <td>{{ row.name }}</td>
                  <td>{{ row.code }}</td>
                  <td>{{ verdictLabel(row.verdict) }}</td>
                  <td>{{ row.reason || '—' }}</td>
                  <td>{{ row.rank ?? '—' }}</td>
                  <td v-for="metric in metricKeys(view)" :key="metric">
                    {{ formatValue(row.metrics[metric]) }}
                  </td>
                  <td>{{ row.override?.reason || '—' }}</td>
                </tr>
                <tr v-if="!rows(view).length">
                  <td :colspan="6 + metricKeys(view).length">暂无结构化结果</td>
                </tr>
              </tbody>
            </table>
          </div>

          <form
            v-if="view.track && view.status === 'waiting_confirmation'"
            class="inline-form override-form"
            @submit.prevent="saveOverride(view)"
          >
            <label>
              覆盖代码
              <input v-model="overrideCode" type="text" name="override_code" required />
            </label>
            <label>
              覆盖动作
              <select v-model="overrideAction" name="override_action">
                <option value="forced_pass">改为通过</option>
                <option value="forced_drop">改为淘汰</option>
              </select>
            </label>
            <label>
              覆盖理由
              <input v-model="overrideReason" type="text" name="override_reason" required />
            </label>
            <button type="submit" :disabled="mutating">保存人工覆盖</button>
          </form>
        </template>

        <div class="stage-actions">
          <button
            v-if="view.status === 'waiting_confirmation'"
            type="button"
            :disabled="mutating"
            @click="stageMutation(view, 'confirm')"
          >
            确认本层
          </button>
          <button
            v-if="view.status === 'failed'"
            type="button"
            :disabled="mutating"
            @click="stageMutation(view, 'retry')"
          >
            重试本层
          </button>
          <span class="muted">仅在等待确认时可确认。</span>
        </div>
      </article>

      <div
        v-if="state.run.status !== 'completed' && state.run.status !== 'partially_completed' && state.run.status !== 'abandoned'"
        class="panel"
      >
        <h2>无法继续？</h2>
        <p class="muted">放弃会保留当前记录；需要修复数据或尺子后重新创建。</p>
        <button type="button" :disabled="mutating" @click="abandonRun">放弃本批次</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.run-summary p {
  margin-bottom: 0.35rem;
}

.stage-panel h2,
.stage-panel h3 {
  margin-top: 0;
}

.stage-panel h3 {
  font-size: 1rem;
  margin-bottom: 0.35rem;
}

.stage-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.75rem;
}

.stage-summary > div,
.summary-details {
  border: 1px solid #e4ddd0;
  padding: 0.7rem;
}

.stage-summary p,
.stage-summary ul {
  margin: 0;
}

.summary-details {
  margin: 0.75rem 0;
}

.summary-details summary {
  cursor: pointer;
  font-weight: 600;
}

.summary-details h3 {
  margin-top: 0.85rem;
}

.table-wrap {
  overflow-x: auto;
  margin: 0.5rem 0 1rem;
}

caption {
  text-align: left;
  font-weight: 600;
  padding: 0.4rem 0;
}

th,
td {
  white-space: nowrap;
}

.override-form {
  border-top: 1px solid #e4ddd0;
  padding-top: 0.75rem;
}

.stage-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.6rem;
  margin-top: 0.75rem;
}

@media (max-width: 640px) {
  .stage-summary {
    grid-template-columns: 1fr;
  }

  .override-form label,
  .override-form input,
  .override-form select {
    width: 100%;
  }
}
</style>
