<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  armFetchTimeout,
  armStageFetchTimeout,
  canAbandonRun,
  collectMetricKeys,
  CURRENT_FETCH_TIMEOUT_MS,
  latestStagesNeedPolling,
  matchesStagePointer,
  parseStageResponse,
  renderMetricValue,
  reviewedStageActions,
  stageAriaLabel,
  stageCacheKey,
  stagePayloadFingerprint,
  sameStageReview,
  shouldPollStage,
  STAGE_POLL_INTERVAL_MS,
  type DiscoveryFailureKind,
  type DiscoveryStageName,
  type DiscoveryStageStatus,
  type DiscoveryTrack,
  type DiscoveryTrackStage,
  type MetricValue,
  type StageReviewIdentity,
} from './discoveryViewModel'

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
type StageStatus = DiscoveryStageStatus
type Track = DiscoveryTrack
type TrackStage = DiscoveryTrackStage
type StageKey = 'safety' | `${Track}:${TrackStage}`

interface RunStagePointer {
  stage: string | null
  status: StageStatus | null
  created: boolean
  attempt?: number
}

interface DiscoveryRun {
  id: number
  current: boolean
  week_key: string
  status: DiscoveryRunStatus
  snapshot_at: string | null
  data_version: string | null
  financial_version: string | null
  valuation_version: string | null
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
  original_verdict: 'pass' | 'drop' | 'insufficient'
  reason: string | null
  rank: number | null
  metrics: Record<string, MetricValue>
  metrics_unavailable_reason: string | null
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
  failure_kind: DiscoveryFailureKind
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
  stage: DiscoveryStageName
  created: boolean
  status: StageStatus
  payload: StageResponse | null
  metricKeys: string[]
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

const RESULT_GROUPS = [
  { key: 'pass', title: '通过结果' },
  { key: 'drop', title: '淘汰结果' },
  { key: 'insufficient', title: '数据不足结果' },
] as const
const CURRENT_REFRESH_MS = 60_000
const CURRENT_RETRY_MS = 5_000

const state = ref<ViewState>({ kind: 'loading' })
const starting = ref(false)
const mutating = ref(false)
const actionError = ref('')
const transitionAnnouncement = ref('')
const stageViews = ref<StageView[]>([])
const activeRunId = ref<number | null>(null)
const activeStageKey = ref<Record<string, number>>({})
const selectedTrackStages = ref<Record<Track, TrackStage>>({
  quality_discount: 'track_gate',
  deep_value: 'track_gate',
  garp: 'track_gate',
})
const pinnedTrackStages = ref<Record<Track, boolean>>({
  quality_discount: false,
  deep_value: false,
  garp: false,
})
const stagePayloadCache = ref<Record<string, StageResponse>>({})
const stageMetricKeysCache = ref<Record<string, string[]>>({})
const stageFingerprintCache = ref<Record<string, string>>({})
const stageReviewableCache = ref<Record<string, boolean>>({})
const reviewedStages = ref<Record<string, StageReviewIdentity>>({})
const overrideCode = ref('')
const overrideAction = ref<'forced_pass' | 'forced_drop'>('forced_pass')
const overrideReason = ref('')
let pollTimer: number | undefined
let currentRefreshTimer: number | undefined
let mounted = false
let currentRequestSequence = 0
let currentController: AbortController | null = null
const stageControllers = new Map<string, AbortController>()

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

function isTrackStage(value: unknown): value is TrackStage {
  return TRACK_STAGES.some((stage) => stage === value)
}

function isRunStagePointer(value: unknown): value is RunStagePointer {
  return isRecord(value)
    && (value.stage === null || typeof value.stage === 'string')
    && (value.status === null || isStageStatus(value.status))
    && typeof value.created === 'boolean'
    && (value.attempt === undefined || nonNegativeInteger(value.attempt) !== null)
}

function isDiscoveryRun(value: unknown): value is DiscoveryRun {
  if (!isRecord(value)) return false
  if (
    typeof value.id !== 'number'
    || !Number.isInteger(value.id)
    || value.id <= 0
    || typeof value.current !== 'boolean'
    || typeof value.week_key !== 'string'
    || !value.week_key
    || !isDiscoveryRunStatus(value.status)
    || (value.snapshot_at !== null && typeof value.snapshot_at !== 'string')
    || (value.data_version !== null && typeof value.data_version !== 'string')
    || (value.financial_version !== null && typeof value.financial_version !== 'string')
    || (value.valuation_version !== null && typeof value.valuation_version !== 'string')
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

function errorMessage(body: unknown, fallback: string): string {
  if (!isRecord(body)) return fallback
  if (typeof body.detail === 'string' && body.detail) return body.detail
  if (typeof body.error === 'string' && body.error) return body.error
  return fallback
}

function announceStageTransitions(previous: DiscoveryRun | null, next: DiscoveryRun) {
  if (previous === null || previous.id !== next.id) {
    transitionAnnouncement.value = ''
    return
  }
  const messages: string[] = []
  if (
    previous.safety?.status !== next.safety?.status
    && next.safety?.status != null
  ) {
    messages.push(`共同排雷状态更新为${STAGE_STATUS_ZH[next.safety.status]}`)
  }
  for (const track of TRACKS) {
    const before = previous.tracks?.[track]
    const after = next.tracks?.[track]
    if (
      after?.status != null
      && (before?.stage !== after.stage || before?.status !== after.status)
    ) {
      const stage = isTrackStage(after.stage) ? STAGE_ZH[after.stage] : '待开始'
      messages.push(
        `${TRACK_ZH[track]}进入${stage}，状态${STAGE_STATUS_ZH[after.status]}`,
      )
    }
  }
  if (messages.length) transitionAnnouncement.value = messages.join('；')
}

function viewsForRun(run: DiscoveryRun): StageView[] {
  const safetyStatus = run.safety?.status ?? 'queued'
  const safetyKey = stageCacheKey(run.id, null, 'safety')
  let safetyPayload = stagePayloadCache.value[safetyKey] ?? null
  if (
    safetyPayload
    && !matchesStagePointer(
      run.safety?.attempt,
      run.safety?.status,
      safetyPayload.current.attempt,
      safetyPayload.current.status,
    )
  ) {
    clearStageCache(safetyKey)
    safetyPayload = null
  }
  const views: StageView[] = [{
    key: 'safety',
    title: '共同排雷',
    track: null,
    stage: 'safety',
    created: run.safety?.created ?? true,
    status: safetyPayload?.current.status ?? safetyStatus,
    payload: safetyPayload,
    metricKeys: stageMetricKeysCache.value[safetyKey] ?? [],
    error: '',
  }]
  for (const track of TRACKS) {
    const pointer = run.tracks?.[track]
    const stage = selectedTrackStages.value[track]
    const cacheKey = stageCacheKey(run.id, track, stage)
    const selectedIsLatest = pointer?.stage === stage
    let payload = stagePayloadCache.value[cacheKey] ?? null
    if (
      payload
      && selectedIsLatest
      && !matchesStagePointer(
        pointer?.attempt,
        pointer?.status,
        payload.current.attempt,
        payload.current.status,
      )
    ) {
      clearStageCache(cacheKey)
      payload = null
    }
    views.push({
      key: `${track}:${stage}`,
      title: `${TRACK_ZH[track]} · ${STAGE_ZH[stage]}`,
      track,
      stage,
      created: payload !== null || (selectedIsLatest && (pointer?.created ?? false)),
      status: payload?.current.status ?? (selectedIsLatest ? pointer?.status ?? 'queued' : 'queued'),
      payload,
      metricKeys: stageMetricKeysCache.value[cacheKey] ?? [],
      error: '',
    })
  }
  return views
}

function endpoint(view: StageView, runId: number): string {
  return view.track === null
    ? `/api/discoveries/${runId}/stages/safety`
    : `/api/discoveries/${runId}/tracks/${view.track}/stages/${view.stage}`
}

function clearReviewedStage(key?: string) {
  if (key === undefined) {
    reviewedStages.value = {}
    return
  }
  const next = { ...reviewedStages.value }
  delete next[key]
  reviewedStages.value = next
}

function clearStageCache(key?: string) {
  if (key === undefined) {
    stagePayloadCache.value = {}
    stageMetricKeysCache.value = {}
    stageFingerprintCache.value = {}
    stageReviewableCache.value = {}
    clearReviewedStage()
    return
  }
  const payloads = { ...stagePayloadCache.value }
  const metricKeys = { ...stageMetricKeysCache.value }
  const fingerprints = { ...stageFingerprintCache.value }
  const reviewable = { ...stageReviewableCache.value }
  delete payloads[key]
  delete metricKeys[key]
  delete fingerprints[key]
  delete reviewable[key]
  stagePayloadCache.value = payloads
  stageMetricKeysCache.value = metricKeys
  stageFingerprintCache.value = fingerprints
  stageReviewableCache.value = reviewable
  clearReviewedStage(key)
}

function reviewIdentity(
  runId: number,
  view: StageView,
  payload: StageResponse,
  fingerprint: string,
): StageReviewIdentity {
  return {
    runId,
    track: view.track,
    stage: view.stage,
    attempt: payload.current.attempt,
    status: payload.current.status,
    fingerprint,
  }
}

function pointerMatchesView(run: DiscoveryRun, view: StageView, payload: StageResponse): boolean {
  const pointer = view.track === null ? run.safety : run.tracks?.[view.track]
  return pointer?.created === true
    && (view.track === null || pointer.stage === view.stage)
    && matchesStagePointer(
      pointer.attempt,
      pointer.status,
      payload.current.attempt,
      payload.current.status,
    )
}

function reviewedIdentityFor(view: StageView): StageReviewIdentity | null {
  const current = state.value
  if (
    current.kind !== 'ready'
    || activeRunId.value !== current.run.id
    || !stageViews.value.includes(view)
    || view.payload === null
    || view.error !== ''
  ) return null
  const key = stageCacheKey(current.run.id, view.track, view.stage)
  const fingerprint = stageFingerprintCache.value[key]
  if (
    !fingerprint
    || stagePayloadCache.value[key] !== view.payload
    || stageReviewableCache.value[key] !== true
    || !pointerMatchesView(current.run, view, view.payload)
  ) return null
  const actual = reviewIdentity(current.run.id, view, view.payload, fingerprint)
  return sameStageReview(reviewedStages.value[key], actual) ? actual : null
}

function currentStageViews(): StageView[] | null {
  const current = state.value
  if (current.kind !== 'ready') return null
  const views: StageView[] = []
  if (current.run.safety?.created) {
    const safety = stageViews.value.find((view) => view.track === null && view.stage === 'safety')
    if (!safety) return null
    views.push(safety)
  }
  for (const track of TRACKS) {
    const pointer = current.run.tracks?.[track]
    if (!pointer?.created) continue
    if (!isTrackStage(pointer.stage)) return null
    const view = stageViews.value.find(
      (candidate) => candidate.track === track && candidate.stage === pointer.stage,
    )
    if (!view) return null
    views.push(view)
  }
  return views.length ? views : null
}

function abandonReviewIdentities(): StageReviewIdentity[] | null {
  const views = currentStageViews()
  if (!views) return null
  const identities = views.map(reviewedIdentityFor)
  return identities.some((identity) => identity === null)
    ? null
    : identities as StageReviewIdentity[]
}

function canAbandonReviewedRun(): boolean {
  const current = state.value
  return current.kind === 'ready'
    && canAbandonRun(current.run.current, current.run.status)
    && abandonReviewIdentities() !== null
}

function stageAvailable(track: Track, stage: TrackStage): boolean {
  const current = state.value
  if (current.kind !== 'ready') return false
  const latest = current.run.tracks?.[track]
  if (!latest?.created || !isTrackStage(latest.stage)) return false
  return TRACK_STAGES.indexOf(stage) <= TRACK_STAGES.indexOf(latest.stage)
}

function stopPoll() {
  if (pollTimer !== undefined) {
    window.clearTimeout(pollTimer)
    pollTimer = undefined
  }
}

function stopCurrentRefresh() {
  if (currentRefreshTimer !== undefined) {
    window.clearTimeout(currentRefreshTimer)
    currentRefreshTimer = undefined
  }
}

function abortStageRequests() {
  for (const controller of stageControllers.values()) controller.abort()
  stageControllers.clear()
}

function needsPolling(): boolean {
  const current = state.value
  if (current.kind !== 'ready') return false
  const pointers = [
    current.run.safety,
    ...TRACKS.map((track) => current.run.tracks?.[track]),
  ]
  return latestStagesNeedPolling(pointers)
}

function schedulePoll() {
  stopPoll()
  if (!mounted || !needsPolling()) return
  pollTimer = window.setTimeout(() => {
    if (mounted) void refreshRun()
  }, STAGE_POLL_INTERVAL_MS)
}

function scheduleCurrentRefresh(delay = CURRENT_REFRESH_MS) {
  stopCurrentRefresh()
  if (!mounted) return
  currentRefreshTimer = window.setTimeout(() => {
    if (mounted) void readCurrent()
  }, delay)
}

async function loadStage(
  view: StageView,
  runId: number,
  force = false,
): Promise<StageResponse | null> {
  const cacheKey = stageCacheKey(runId, view.track, view.stage)
  if (stageControllers.has(cacheKey)) {
    if (!force) return null
    stageControllers.get(cacheKey)?.abort()
  }
  if (force) clearReviewedStage(cacheKey)
  const requestNumber = (activeStageKey.value[cacheKey] ?? 0) + 1
  activeStageKey.value[cacheKey] = requestNumber
  const controller = new AbortController()
  let timedOut = false
  const cancelTimeout = armStageFetchTimeout(controller, () => {
    timedOut = true
  })
  stageControllers.set(cacheKey, controller)
  try {
    const response = await fetch(endpoint(view, runId), { signal: controller.signal })
    const body: unknown = await response.json()
    if (
      !mounted
      || activeRunId.value !== runId
      || activeStageKey.value[cacheKey] !== requestNumber
    ) return null
    const current = stageViews.value.find((item) => item.key === view.key)
    if (!current) return null
    if (response.status === 404) {
      clearStageCache(cacheKey)
      current.payload = null
      current.metricKeys = []
      current.created = false
      current.error = shouldPollStage(view.created, view.status) ? '' : '阶段尚未生成'
      return null
    }
    if (!response.ok) {
      clearStageCache(cacheKey)
      current.payload = null
      current.metricKeys = []
      current.error = errorMessage(body, '无法读取阶段结果')
      return null
    }
    const payload = parseStageResponse(body)
    if (!payload) {
      clearStageCache(cacheKey)
      current.payload = null
      current.metricKeys = []
      current.error = '服务返回的阶段数据无效'
      return null
    }
    const resultRows = [
      ...payload.pass,
      ...payload.drop,
      ...payload.insufficient,
    ]
    const metricKeys = collectMetricKeys(resultRows)
    stagePayloadCache.value = { ...stagePayloadCache.value, [cacheKey]: payload }
    stageMetricKeysCache.value = {
      ...stageMetricKeysCache.value,
      [cacheKey]: metricKeys,
    }
    const fingerprint = stagePayloadFingerprint(payload)
    stageFingerprintCache.value = {
      ...stageFingerprintCache.value,
      [cacheKey]: fingerprint,
    }
    stageReviewableCache.value = {
      ...stageReviewableCache.value,
      [cacheKey]: resultRows.every((row) => row.metrics_unavailable_reason === null),
    }
    reviewedStages.value = {
      ...reviewedStages.value,
      [cacheKey]: reviewIdentity(runId, current, payload, fingerprint),
    }
    current.payload = payload
    current.metricKeys = metricKeys
    current.created = true
    current.status = payload.current.status
    current.error = ''
    return payload
  } catch (error) {
    if (!timedOut && error instanceof DOMException && error.name === 'AbortError') return null
    if (
      !mounted
      || activeRunId.value !== runId
      || activeStageKey.value[cacheKey] !== requestNumber
    ) return null
    clearStageCache(cacheKey)
    const current = stageViews.value.find((item) => item.key === view.key)
    if (current) {
      current.payload = null
      current.metricKeys = []
      current.error = timedOut ? '阶段读取超时，将在下一轮重试' : '无法读取阶段结果'
    }
    return null
  } finally {
    cancelTimeout()
    if (stageControllers.get(cacheKey) === controller) stageControllers.delete(cacheKey)
  }
}

async function applyRun(
  run: DiscoveryRun,
  source: 'loaded' | 'created',
  requestSequence: number,
) {
  stopPoll()
  const previousRun = state.value.kind === 'ready' ? state.value.run : null
  announceStageTransitions(previousRun, run)
  const changedRun = activeRunId.value !== run.id
  if (changedRun) {
    abortStageRequests()
    clearStageCache()
    for (const track of TRACKS) {
      const latest = run.tracks?.[track]?.stage
      selectedTrackStages.value[track] = isTrackStage(latest) ? latest : 'track_gate'
      pinnedTrackStages.value[track] = false
    }
  } else {
    for (const track of TRACKS) {
      const latest = run.tracks?.[track]?.stage
      if (!pinnedTrackStages.value[track] && isTrackStage(latest)) {
        selectedTrackStages.value[track] = latest
      }
    }
  }
  activeRunId.value = run.id
  state.value = { kind: 'ready', run, source }
  stageViews.value = viewsForRun(run)
  if (!mounted || requestSequence !== currentRequestSequence || activeRunId.value !== run.id) return
  schedulePoll()
  scheduleCurrentRefresh()
  void Promise.allSettled(stageViews.value.map((view) => loadStage(view, run.id)))
}

async function readCurrent(source: 'loaded' | 'created' = 'loaded'): Promise<boolean> {
  const requestSequence = ++currentRequestSequence
  currentController?.abort()
  const controller = new AbortController()
  let timedOut = false
  const cancelTimeout = armFetchTimeout(controller, CURRENT_FETCH_TIMEOUT_MS, () => {
    timedOut = true
  })
  currentController = controller
  try {
    const response = await fetch('/api/discoveries/current', { signal: controller.signal })
    if (!mounted || requestSequence !== currentRequestSequence) return false
    if (response.status === 404) {
      stopPoll()
      abortStageRequests()
      clearStageCache()
      activeRunId.value = null
      stageViews.value = []
      transitionAnnouncement.value = ''
      state.value = { kind: 'empty' }
      scheduleCurrentRefresh()
      return false
    }
    const body: unknown = await response.json()
    if (!mounted || requestSequence !== currentRequestSequence) return false
    if (!response.ok) {
      abortStageRequests()
      clearStageCache()
      state.value = { kind: 'error', message: errorMessage(body, '无法读取本周发现') }
    } else if (!isDiscoveryRun(body)) {
      abortStageRequests()
      clearStageCache()
      state.value = { kind: 'error', message: '服务返回的本周批次数据无效' }
    } else {
      await applyRun(body, source, requestSequence)
      return true
    }
  } catch (error) {
    if (!timedOut && error instanceof DOMException && error.name === 'AbortError') return false
    if (!mounted || requestSequence !== currentRequestSequence) return false
    abortStageRequests()
    clearStageCache()
    state.value = {
      kind: 'error',
      message: timedOut ? '读取本周批次超时，将自动重试' : '无法读取本周发现',
    }
  } finally {
    cancelTimeout()
    if (currentController === controller) currentController = null
  }
  stopPoll()
  scheduleCurrentRefresh(CURRENT_RETRY_MS)
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
      await readCurrent('created')
    }
  } catch {
    state.value = { kind: 'error', message: '无法开始本周发现' }
  } finally {
    starting.value = false
  }
}

async function beforeMutation(
  expected: StageReviewIdentity[],
  requireAllCurrentStages = false,
): Promise<StageView[] | null> {
  if (
    !expected.length
    || expected.some((identity) => !sameStageReview(
      reviewedStages.value[stageCacheKey(identity.runId, identity.track, identity.stage)],
      identity,
    ))
  ) {
    actionError.value = '当前阶段尚未成功加载并复核'
    return null
  }
  const expectedRunId = expected[0]!.runId
  const refreshed = await readCurrent()
  if (!refreshed || activeRunId.value !== expectedRunId || state.value.kind !== 'ready') {
    actionError.value = '周次或当前批次已变化，已重新加载，请复核后再操作'
    return null
  }
  const views: StageView[] = []
  for (const identity of expected) {
    const view = stageViews.value.find(
      (candidate) => candidate.track === identity.track && candidate.stage === identity.stage,
    )
    if (!view) {
      actionError.value = '阶段已变化，请重新选择并复核'
      return null
    }
    const pointer = view.track === null
      ? state.value.run.safety
      : state.value.run.tracks?.[view.track]
    if (
      pointer?.created !== true
      || (view.track !== null && pointer.stage !== view.stage)
      || pointer.attempt !== identity.attempt
      || pointer.status !== identity.status
    ) {
      actionError.value = '阶段身份或状态已变化，请复核最新结果'
      return null
    }
    views.push(view)
  }
  if (requireAllCurrentStages) {
    const allCurrent = currentStageViews()
    const expectedKeys = new Set(expected.map(
      (identity) => stageCacheKey(identity.runId, identity.track, identity.stage),
    ))
    if (
      !allCurrent
      || allCurrent.length !== expected.length
      || allCurrent.some((view) => !expectedKeys.has(
        stageCacheKey(expectedRunId, view.track, view.stage),
      ))
    ) {
      actionError.value = '当前阶段集合已变化，请复核最新结果'
      return null
    }
  }
  const payloads = await Promise.all(
    views.map((view) => loadStage(view, expectedRunId, true)),
  )
  const actual = views.map(reviewedIdentityFor)
  if (
    payloads.some((payload) => payload === null)
    || expected.some((identity, index) => !sameStageReview(identity, actual[index]))
  ) {
    actionError.value = '阶段结果已变化或无法验证，请复核最新结果后重试'
    return null
  }
  return views
}

async function stageMutation(view: StageView, action: 'confirm' | 'retry') {
  if (mutating.value || activeRunId.value === null) return
  const expected = reviewedIdentityFor(view)
  if (!expected) {
    actionError.value = '当前阶段尚未成功加载并复核'
    return
  }
  const runId = expected.runId
  actionError.value = ''
  mutating.value = true
  try {
    const currentView = (await beforeMutation([expected]))?.[0]
    if (!currentView) return
    const allowed = actionsFor(currentView)
    if ((action === 'confirm' && !allowed.confirm) || (action === 'retry' && !allowed.retry)) {
      actionError.value = '当前阶段状态不允许此操作'
      return
    }
    clearReviewedStage(stageCacheKey(runId, currentView.track, currentView.stage))
    const response = await fetch(`${endpoint(currentView, runId)}/${action}`, {
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
  const expected = reviewedIdentityFor(view)
  if (!expected || expected.track === null || expected.stage === 'safety') {
    actionError.value = '当前阶段尚未成功加载并复核'
    return
  }
  const runId = expected.runId
  const track = expected.track
  const stage = expected.stage
  const reason = overrideReason.value.trim()
  const code = overrideCode.value.trim()
  if (!code || !reason) {
    actionError.value = '代码和覆盖理由均为必填'
    return
  }
  actionError.value = ''
  mutating.value = true
  try {
    const currentView = (await beforeMutation([expected]))?.[0]
    if (!currentView || !actionsFor(currentView).override) {
      actionError.value = '当前阶段不可人工覆盖'
      return
    }
    clearReviewedStage(stageCacheKey(runId, track, stage))
    const response = await fetch(`/api/discoveries/${runId}/overrides`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        track,
        stage,
        attempt: currentView.payload?.current.attempt,
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
      await loadStage(currentView, runId)
    }
  } catch {
    actionError.value = '人工覆盖保存失败'
  } finally {
    mutating.value = false
  }
}

async function abandonRun() {
  if (mutating.value || activeRunId.value === null) return
  const expected = abandonReviewIdentities()
  if (!expected) {
    actionError.value = '请先成功加载并复核所有当前阶段'
    return
  }
  const runId = expected[0]!.runId
  actionError.value = ''
  mutating.value = true
  try {
    if (!await beforeMutation(expected, true)) return
    clearReviewedStage()
    const response = await fetch(`/api/discoveries/${runId}/abandon`, {
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

async function onStageSelection(track: Track) {
  const current = state.value
  if (current.kind !== 'ready') return
  pinnedTrackStages.value[track] = true
  stageViews.value = viewsForRun(current.run)
  const view = stageViews.value.find((item) => item.track === track)
  if (view) await loadStage(view, current.run.id)
  schedulePoll()
}

function rows(view: StageView): DiscoveryRow[] {
  const payload = view.payload
  return payload ? [...payload.pass, ...payload.drop, ...payload.insufficient] : []
}

function resultRows(view: StageView, key: 'pass' | 'drop' | 'insufficient'): DiscoveryRow[] {
  return view.payload?.[key] ?? []
}

function overrideResult(view: StageView, code: string): DiscoveryRow | undefined {
  return rows(view).find((row) => row.code === code)
}

function overrideDirection(override: DiscoveryOverride): string {
  return override.action === 'forced_pass' ? '改为通过' : '改为淘汰'
}

function overrideVerdict(view: StageView, code: string, original: boolean): string {
  const row = overrideResult(view, code)
  return row ? verdictLabel(original ? row.original_verdict : row.verdict) : '不可用'
}

function actionsFor(view: StageView) {
  return reviewedStageActions(
    reviewedIdentityFor(view) !== null,
    view.stage,
    view.status,
    view.payload?.current.failure_kind ?? null,
  )
}

function verdictLabel(verdict: DiscoveryRow['verdict']): string {
  return { pass: '通过', drop: '淘汰', insufficient: '数据不足' }[verdict]
}

function formatValue(value: MetricValue | undefined): string {
  return value === undefined ? '不可用' : renderMetricValue(value)
}

function formatTimestamp(value: string | null): string {
  if (!value) return '不可用'
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp)
    ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(timestamp)
    : '不可用'
}

function markerPosition(marker: ThresholdMarker): string {
  if (marker.status !== 'available' || marker.position === null) return '不可用'
  const position = Number(marker.position)
  return Number.isFinite(position) ? `${position}%` : '不可用'
}

onMounted(() => {
  mounted = true
  void loadCurrent()
})

onBeforeUnmount(() => {
  mounted = false
  currentRequestSequence += 1
  currentController?.abort()
  currentController = null
  abortStageRequests()
  activeRunId.value = null
  stopPoll()
  stopCurrentRefresh()
})
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>每周发现</h1>
      <p class="muted">按固定尺子筛选，再逐层确认。</p>
    </div>

    <p role="status" aria-live="polite" aria-atomic="true">{{ announcement }}</p>
    <p class="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {{ transitionAnnouncement }}
    </p>

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
        <p>
          快照时间：{{ formatTimestamp(state.run.snapshot_at) }}　
          数据版本：{{ state.run.data_version || '不可用' }}　
          财务版本：{{ state.run.financial_version || '不可用' }}　
          估值版本：{{ state.run.valuation_version || '不可用' }}
        </p>
        <p v-if="state.run.error" role="alert">{{ state.run.error }}</p>
      </div>

      <p v-if="actionError" role="alert">{{ actionError }}</p>

      <article
        v-for="view in stageViews"
        :key="view.key"
        class="panel stage-panel"
        :aria-busy="view.created && (view.status === 'queued' || view.status === 'running')"
        :aria-labelledby="`discovery-stage-${view.key.replace(':', '-')}`"
      >
        <div class="toolbar">
          <h2 :id="`discovery-stage-${view.key.replace(':', '-')}`">{{ view.title }}</h2>
          <p class="muted">状态：{{ STAGE_STATUS_ZH[view.status] }}</p>
          <label v-if="view.track" class="stage-selector">
            查看阶段
            <select
              v-model="selectedTrackStages[view.track]"
              @change="onStageSelection(view.track)"
            >
              <option
                v-for="stage in TRACK_STAGES"
                :key="stage"
                :value="stage"
                :disabled="!stageAvailable(view.track, stage)"
              >
                {{ STAGE_ZH[stage] }}
              </option>
            </select>
          </label>
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
        <p v-if="view.stage === 'adjudication'" class="muted">
          人工裁决将在 Task 4 接入逐项决策；当前只读，不提供确认或人工覆盖。
        </p>

        <template v-if="view.payload">
          <section class="stage-summary" :aria-label="stageAriaLabel(view.title, '阶段摘要')">
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
            <div
              class="table-wrap"
              tabindex="0"
              :aria-label="stageAriaLabel(view.title, '指标分布，可横向滚动')"
            >
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

          <div
            v-for="group in RESULT_GROUPS"
            :key="group.key"
            class="table-wrap"
            tabindex="0"
            :aria-label="stageAriaLabel(view.title, `${group.title}，可横向滚动`)"
          >
            <table>
              <caption>{{ view.title }} · {{ group.title }}</caption>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>代码</th>
                  <th>原判定</th>
                  <th>有效判定</th>
                  <th>理由</th>
                  <th>排名</th>
                  <th>指标状态</th>
                  <th v-for="metric in view.metricKeys" :key="metric">{{ metric }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in resultRows(view, group.key)" :key="row.code">
                  <td>{{ row.name }}</td>
                  <td>{{ row.code }}</td>
                  <td>{{ verdictLabel(row.original_verdict) }}</td>
                  <td>{{ verdictLabel(row.verdict) }}</td>
                  <td>{{ row.reason || '—' }}</td>
                  <td>{{ row.rank ?? '—' }}</td>
                  <td>{{ row.metrics_unavailable_reason || '可用' }}</td>
                  <td v-for="metric in view.metricKeys" :key="metric">
                    {{ formatValue(row.metrics[metric]) }}
                  </td>
                </tr>
                <tr v-if="!resultRows(view, group.key).length">
                  <td :colspan="7 + view.metricKeys.length">暂无{{ group.title }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div
            class="table-wrap"
            tabindex="0"
            :aria-label="stageAriaLabel(view.title, '人工覆盖记录，可横向滚动')"
          >
            <table>
              <caption>{{ view.title }} · 人工覆盖记录</caption>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>代码</th>
                  <th>覆盖方向</th>
                  <th>原判定</th>
                  <th>有效判定</th>
                  <th>覆盖理由</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="override in view.payload.overrides" :key="override.id">
                  <td>{{ overrideResult(view, override.code)?.name || override.code }}</td>
                  <td>{{ override.code }}</td>
                  <td>{{ overrideDirection(override) }}</td>
                  <td>{{ overrideVerdict(view, override.code, true) }}</td>
                  <td>{{ overrideVerdict(view, override.code, false) }}</td>
                  <td>{{ override.reason }}</td>
                </tr>
                <tr v-if="!view.payload.overrides.length">
                  <td colspan="6">暂无人工覆盖记录</td>
                </tr>
              </tbody>
            </table>
          </div>

          <form
            v-if="view.track && actionsFor(view).override"
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
            v-if="actionsFor(view).confirm"
            type="button"
            :disabled="mutating"
            @click="stageMutation(view, 'confirm')"
          >
            确认本层
          </button>
          <button
            v-if="actionsFor(view).retry"
            type="button"
            :disabled="mutating"
            @click="stageMutation(view, 'retry')"
          >
            重试本层
          </button>
          <span v-if="view.stage !== 'adjudication'" class="muted">
            仅在等待确认时可确认。
          </span>
        </div>
      </article>

      <div
        v-if="canAbandonReviewedRun()"
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
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

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

.stage-selector {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
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
