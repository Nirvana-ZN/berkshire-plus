<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  armFetchTimeout,
  armStageFetchTimeout,
  canAbandonRun,
  collectMetricKeys,
  candidateSurfacedReason,
  formatEvidenceText,
  CURRENT_FETCH_TIMEOUT_MS,
  discoveryChangeLabels,
  keeperRange,
  latestStagesNeedPolling,
  matchesStagePointer,
  parseStageResponse,
  preferredViewStage,
  renderMetricValue,
  stageIsReachable,
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
  type ParsedDiscoveryChange,
  type ParsedDiscoveryDecision,
  type ParsedDiscoveryEvidence,
  type StageReviewIdentity,
  validateCandidateEvidence,
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
const VISIBLE_TRACK_STAGES = ['track_gate', 'competition'] as const

type DiscoveryRunStatus = (typeof DISCOVERY_RUN_STATUSES)[number]
type StageStatus = DiscoveryStageStatus
type Track = DiscoveryTrack
type TrackStage = DiscoveryTrackStage
type StageKey = 'safety' | `${Track}:${TrackStage}`

interface RunStagePointer {
  id?: number
  stage: string | null
  status: StageStatus | null
  created: boolean
  attempt?: number
  input_fingerprint?: string
  ruler_version_id?: number | null
  stage_revision?: number
  mutation_version?: string
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
  id: number
  track: Track | null
  stage: DiscoveryStageName
  attempt: number
  status: StageStatus
  error: string | null
  failure_kind: DiscoveryFailureKind
  input_fingerprint: string
  ruler_version_id: number | null
  stage_revision: number
  mutation_version: string
}

interface StageResponse {
  current: StageCurrent
  pass: DiscoveryRow[]
  drop: DiscoveryRow[]
  insufficient: DiscoveryRow[]
  overrides: DiscoveryOverride[]
  decisions: ParsedDiscoveryDecision[]
  evidence: ParsedDiscoveryEvidence[]
  changes: ParsedDiscoveryChange[]
  summary: StageSummary
}

interface AdjudicationCase {
  code: string
  name: string
  surfacedReason: string
  overridden: boolean
  track: Track
  evidence: ParsedDiscoveryEvidence[]
  decision: ParsedDiscoveryDecision | null
  change: ParsedDiscoveryChange | null
}

type CandidatePayloadState =
  | { kind: 'loading' }
  | { kind: 'ready' }
  | { kind: 'error'; message: string }

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

interface ValidatedStage {
  view: StageView
  identity: StageReviewIdentity
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
const MUTATION_FETCH_TIMEOUT_MS = 10_000
const CHANGE_STATE_ZH: Record<ParsedDiscoveryChange['previous_state'], string> = {
  absent: '未出现',
  pass: '入围',
  drop: '未入围',
  insufficient: '数据不足',
}

const router = useRouter()
const state = ref<ViewState>({ kind: 'loading' })
const starting = ref(false)
const mutating = ref(false)
const actionError = ref('')
const researchErrorCode = ref('')
const fillStatus = ref<{
  status: string
  snapshotStatus: string | null
  total: number
  completed: number
  failed: number
  pending: number
  lastError: string | null
  dataAsOf: string | null
} | null>(null)
const fillStarting = ref(false)
let fillTimer: ReturnType<typeof setInterval> | null = null
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
const adjudicationPayloads = ref<Partial<Record<Track, StageResponse>>>({})
const candidatePayloadStates = ref<Partial<Record<Track, CandidatePayloadState>>>({})
const exclusionReasons = ref<Record<string, string>>({})
const researchingCode = ref('')
const overrideCode = ref('')
const overrideAction = ref<'forced_pass' | 'forced_drop'>('forced_pass')
const overrideReason = ref('')
let pollTimer: number | undefined
let currentRefreshTimer: number | undefined
let mounted = false
let currentRequestSequence = 0
let currentController: AbortController | null = null
let mutationRequestSequence = 0
let mutationController: AbortController | null = null
const stageControllers = new Map<string, AbortController>()
const stageTimeoutRetries = new Map<string, number>()
const expandedGroups = ref<Record<string, boolean>>({})
const activeStageFilter = ref<'all' | 'safety' | Track>('quality_discount')
const STAGE_TIMEOUT_RETRY_LIMIT = 2
const adjudicationControllers = new Map<Track, AbortController>()
let adjudicationRequestSequence = 0

const announcement = computed(() => {
  if (starting.value) return '正在创建本周发现…'
  const current = state.value
  if (current.kind === 'loading') return '正在读取本周批次…'
  if (current.kind === 'empty') {
    return fillBlocksStart.value
      ? '全市场数据快照尚未完成，还不能开始本周发现。'
      : '本周尚未创建批次。可以开始本周发现。'
  }
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
    && (
      value.created === false
      || (
        nonNegativeInteger(value.id) !== null
        && typeof value.input_fingerprint === 'string'
        && !!value.input_fingerprint
        && (
          value.ruler_version_id === null
          || nonNegativeInteger(value.ruler_version_id) !== null
        )
        && typeof value.mutation_version === 'string'
        && !!value.mutation_version
        && typeof value.stage_revision === 'number'
        && Number.isSafeInteger(value.stage_revision)
        && value.stage_revision >= 0
      )
    )
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
      created: payload !== null || stageIsReachable(stage, pointer),
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
    stageId: payload.current.id,
    attempt: payload.current.attempt,
    status: payload.current.status,
    inputFingerprint: payload.current.input_fingerprint,
    rulerVersionId: payload.current.ruler_version_id,
    stageRevision: payload.current.stage_revision,
    mutationVersion: payload.current.mutation_version,
    fingerprint,
  }
}

function stageExpectation(identity: StageReviewIdentity) {
  return {
    expected_stage_id: identity.stageId,
    expected_stage_attempt: identity.attempt,
    expected_stage_status: identity.status,
    expected_input_fingerprint: identity.inputFingerprint,
    expected_ruler_version_id: identity.rulerVersionId,
    expected_stage_revision: identity.stageRevision,
    expected_mutation_version: identity.mutationVersion,
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
    && pointer.id === payload.current.id
    && pointer.input_fingerprint === payload.current.input_fingerprint
    && pointer.ruler_version_id === payload.current.ruler_version_id
    && pointer.stage_revision === payload.current.stage_revision
    && pointer.mutation_version === payload.current.mutation_version
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

function stageAvailable(track: Track, stage: TrackStage): boolean {
  const current = state.value
  if (current.kind !== 'ready') return false
  if (!VISIBLE_TRACK_STAGES.includes(stage as (typeof VISIBLE_TRACK_STAGES)[number])) {
    return false
  }
  return stageIsReachable(stage, current.run.tracks?.[track])
}

function upstreamComputing(view: StageView): boolean {
  const current = state.value
  if (current.kind !== 'ready' || view.track === null) return false
  const pointer = current.run.tracks?.[view.track]
  if (!pointer?.created || !isTrackStage(pointer.stage)) return false
  if (view.stage === 'safety') return false
  const ahead = TRACK_STAGES.indexOf(pointer.stage) > TRACK_STAGES.indexOf(view.stage)
  return ahead && shouldPollStage(true, pointer.status ?? 'queued')
}

async function selectTrackStage(track: Track, stage: TrackStage) {
  if (!stageAvailable(track, stage)) return
  selectedTrackStages.value = { ...selectedTrackStages.value, [track]: stage }
  pinnedTrackStages.value = { ...pinnedTrackStages.value, [track]: true }
  const current = state.value
  if (current.kind !== 'ready') return
  stageViews.value = viewsForRun(current.run)
  const view = stageViews.value.find((item) => item.track === track && item.stage === stage)
  if (view) await loadStage(view, current.run.id)
  schedulePoll()
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
  for (const [key, controller] of stageControllers) {
    activeStageKey.value[key] = (activeStageKey.value[key] ?? 0) + 1
    controller.abort()
  }
  stageControllers.clear()
}

function abortAdjudicationRequests() {
  adjudicationRequestSequence += 1
  for (const controller of adjudicationControllers.values()) controller.abort()
  adjudicationControllers.clear()
}

function beginMutation(): boolean {
  if (mutating.value) return false
  mutating.value = true
  stopPoll()
  stopCurrentRefresh()
  currentRequestSequence += 1
  currentController?.abort()
  currentController = null
  abortStageRequests()
  abortAdjudicationRequests()
  mutationRequestSequence += 1
  mutationController?.abort()
  mutationController = null
  return true
}

function endMutation() {
  mutating.value = false
  if (!mounted) return
  schedulePoll()
  const current = state.value
  scheduleCurrentRefresh(
    current.kind === 'error' ? CURRENT_RETRY_MS : CURRENT_REFRESH_MS,
  )
  if (current.kind === 'ready') {
    void loadStagesInOrder(current.run.id).then(() => loadAdjudicationPayloads(current.run))
  }
}

async function postMutation(
  url: string,
  body: Record<string, unknown>,
): Promise<{ response: Response; body: unknown } | null> {
  const requestSequence = mutationRequestSequence
  const controller = new AbortController()
  const cancelTimeout = armFetchTimeout(controller, MUTATION_FETCH_TIMEOUT_MS)
  mutationController = controller
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    })
    const responseBody: unknown = await response.json()
    return mounted && requestSequence === mutationRequestSequence
      ? { response, body: responseBody }
      : null
  } finally {
    cancelTimeout()
    if (mutationController === controller) mutationController = null
  }
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
  if (!mounted || mutating.value || !needsPolling()) return
  pollTimer = window.setTimeout(() => {
    if (mounted && !mutating.value) void refreshRun()
  }, STAGE_POLL_INTERVAL_MS)
}

function scheduleCurrentRefresh(delay = CURRENT_REFRESH_MS) {
  stopCurrentRefresh()
  if (!mounted || mutating.value) return
  currentRefreshTimer = window.setTimeout(() => {
    if (mounted && !mutating.value) void readCurrent()
  }, delay)
}

function groupShown(view: StageView, group: string): boolean {
  return expandedGroups.value[`${view.key}:${group}`] === true
}

function groupCollapsed(view: StageView, group: string): boolean {
  if (groupShown(view, group)) return false
  // Safety passes are the whole investable universe. Track passes stay open.
  return group !== 'pass' || view.track === null
}

function showGroup(view: StageView, group: string) {
  expandedGroups.value = {
    ...expandedGroups.value,
    [`${view.key}:${group}`]: true,
  }
}

function scheduleStageRetry(
  view: StageView,
  runId: number,
  cacheKey: string,
  requestNumber: number,
) {
  const attempts = stageTimeoutRetries.get(cacheKey) ?? 0
  if (attempts >= STAGE_TIMEOUT_RETRY_LIMIT) return
  stageTimeoutRetries.set(cacheKey, attempts + 1)
  window.setTimeout(() => {
    if (
      !mounted
      || mutating.value
      || activeRunId.value !== runId
      || activeStageKey.value[cacheKey] !== requestNumber
    ) return
    void loadStage(view, runId)
  }, STAGE_POLL_INTERVAL_MS)
}

async function loadStage(
  view: StageView,
  runId: number,
  force = false,
): Promise<StageResponse | null> {
  if (mutating.value && !force) return null
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
    stageTimeoutRetries.delete(cacheKey)
    if (
      view.track !== null
      && view.stage === 'competition'
      && validateCandidateEvidence(payload).valid
    ) {
      adjudicationPayloads.value = {
        ...adjudicationPayloads.value,
        [view.track]: payload,
      }
      candidatePayloadStates.value = {
        ...candidatePayloadStates.value,
        [view.track]: { kind: 'ready' },
      }
    }
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
    if (timedOut) scheduleStageRetry(view, runId, cacheKey, requestNumber)
    return null
  } finally {
    cancelTimeout()
    if (stageControllers.get(cacheKey) === controller) stageControllers.delete(cacheKey)
  }
}

async function loadAdjudicationPayload(track: Track, runId: number) {
  adjudicationControllers.get(track)?.abort()
  const nextPayloads = { ...adjudicationPayloads.value }
  delete nextPayloads[track]
  adjudicationPayloads.value = nextPayloads
  candidatePayloadStates.value = {
    ...candidatePayloadStates.value,
    [track]: { kind: 'loading' },
  }
  const requestSequence = adjudicationRequestSequence
  const controller = new AbortController()
  let timedOut = false
  const cancelTimeout = armStageFetchTimeout(controller, () => {
    timedOut = true
  })
  adjudicationControllers.set(track, controller)
  try {
    const response = await fetch(
      `/api/discoveries/${runId}/tracks/${track}/stages/competition`,
      { signal: controller.signal },
    )
    const body: unknown = await response.json()
    if (
      !mounted
      || activeRunId.value !== runId
      || requestSequence !== adjudicationRequestSequence
      || adjudicationControllers.get(track) !== controller
    ) return
    if (!response.ok) {
      candidatePayloadStates.value = {
        ...candidatePayloadStates.value,
        [track]: {
          kind: 'error',
          message: `${errorMessage(body, '无法读取候选案例')}，请重试`,
        },
      }
      return
    }
    const payload = parseStageResponse(body)
    if (!payload) {
      candidatePayloadStates.value = {
        ...candidatePayloadStates.value,
        [track]: { kind: 'error', message: '服务返回的候选案例数据无效，请重试' },
      }
      return
    }
    const validation = validateCandidateEvidence(payload)
    if (!validation.valid) {
      candidatePayloadStates.value = {
        ...candidatePayloadStates.value,
        [track]: {
          kind: 'error',
          message: `候选案例不可用于裁决：${validation.message}`,
        },
      }
      return
    }
    adjudicationPayloads.value = {
      ...adjudicationPayloads.value,
      [track]: payload,
    }
    candidatePayloadStates.value = {
      ...candidatePayloadStates.value,
      [track]: { kind: 'ready' },
    }
  } catch (error) {
    if (
      !mounted
      || activeRunId.value !== runId
      || requestSequence !== adjudicationRequestSequence
      || adjudicationControllers.get(track) !== controller
    ) return
    candidatePayloadStates.value = {
      ...candidatePayloadStates.value,
      [track]: {
        kind: 'error',
        message: timedOut ? '候选案例读取超时，请重试' : '无法读取候选案例，请重试',
      },
    }
  } finally {
    cancelTimeout()
    if (adjudicationControllers.get(track) === controller) {
      adjudicationControllers.delete(track)
    }
  }
}

function retryAdjudicationPayload(view: StageView) {
  const current = state.value
  if (view.track !== null && current.kind === 'ready') {
    void loadAdjudicationPayload(view.track, current.run.id)
  }
}

// Stage payloads are CPU-bound on one server process: requested together, each
// (and the /current refresh) waits for all the others and hits its timeout.
async function loadStagesInOrder(runId: number) {
  for (const view of [...stageViews.value]) {
    if (!view.created || view.payload !== null) continue
    await loadStage(view, runId).catch(() => null)
  }
}

async function loadAdjudicationPayloads(run: DiscoveryRun) {
  for (const track of TRACKS.filter((item) => trackShowsCandidateCases(item, run))) {
    if (adjudicationPayloads.value[track] || adjudicationControllers.has(track)) continue
    const cached = stagePayloadCache.value[stageCacheKey(run.id, track, 'competition')]
    if (cached && validateCandidateEvidence(cached).valid) {
      adjudicationPayloads.value = { ...adjudicationPayloads.value, [track]: cached }
      candidatePayloadStates.value = { ...candidatePayloadStates.value, [track]: { kind: 'ready' } }
      continue
    }
    await loadAdjudicationPayload(track, run.id).catch(() => undefined)
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
    abortAdjudicationRequests()
    clearStageCache()
    adjudicationPayloads.value = {}
    candidatePayloadStates.value = {}
    for (const track of TRACKS) {
      const pointer = run.tracks?.[track]
      selectedTrackStages.value[track] = preferredViewStage(
        isTrackStage(pointer?.stage) ? pointer.stage : null,
        pointer?.status ?? null,
      )
      pinnedTrackStages.value[track] = false
    }
  } else {
    for (const track of TRACKS) {
      const pointer = run.tracks?.[track]
      if (!pinnedTrackStages.value[track]) {
        selectedTrackStages.value[track] = preferredViewStage(
          isTrackStage(pointer?.stage) ? pointer.stage : null,
          pointer?.status ?? null,
        )
      }
    }
  }
  activeRunId.value = run.id
  state.value = { kind: 'ready', run, source }
  stageViews.value = viewsForRun(run)
  if (!mounted || requestSequence !== currentRequestSequence || activeRunId.value !== run.id) return
  schedulePoll()
  scheduleCurrentRefresh()
  if (!mutating.value) {
    void loadStagesInOrder(run.id).then(() => loadAdjudicationPayloads(run))
  }
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
      abortAdjudicationRequests()
      clearStageCache()
      adjudicationPayloads.value = {}
      candidatePayloadStates.value = {}
      activeRunId.value = null
      stageViews.value = []
      transitionAnnouncement.value = ''
      state.value = { kind: 'empty' }
      watchFill()
      scheduleCurrentRefresh()
      return false
    }
    const body: unknown = await response.json()
    if (!mounted || requestSequence !== currentRequestSequence) return false
    if (!response.ok) {
      abortStageRequests()
      abortAdjudicationRequests()
      clearStageCache()
      state.value = { kind: 'error', message: errorMessage(body, '无法读取本周发现') }
    } else if (!isDiscoveryRun(body)) {
      abortStageRequests()
      abortAdjudicationRequests()
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
    abortAdjudicationRequests()
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
      if (isRecord(body) && (body.code === 'DATA_NOT_READY' || body.code === 'JOB_LOCKED')) {
        state.value = { kind: 'empty' }
        watchFill()
      } else {
        state.value = { kind: 'error', message: errorMessage(body, '无法开始本周发现') }
      }
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
): Promise<ValidatedStage[] | null> {
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
      || pointer.id !== identity.stageId
      || pointer.attempt !== identity.attempt
      || pointer.status !== identity.status
      || pointer.input_fingerprint !== identity.inputFingerprint
      || pointer.ruler_version_id !== identity.rulerVersionId
      || pointer.stage_revision !== identity.stageRevision
      || pointer.mutation_version !== identity.mutationVersion
    ) {
      actionError.value = '阶段身份或状态已变化，请复核最新结果'
      return null
    }
    views.push(view)
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
  return views.map((view, index) => ({
    view,
    identity: actual[index] as StageReviewIdentity,
  }))
}

async function stageMutation(view: StageView, action: 'confirm' | 'retry') {
  if (mutating.value || activeRunId.value === null) return
  const expected = reviewedIdentityFor(view)
  if (!expected) {
    actionError.value = '当前阶段尚未成功加载并复核'
    return
  }
  if (!beginMutation()) return
  actionError.value = ''
  try {
    const validated = (await beforeMutation([expected]))?.[0]
    if (!validated) return
    const allowed = actionsFor(validated.view)
    if ((action === 'confirm' && !allowed.confirm) || (action === 'retry' && !allowed.retry)) {
      actionError.value = '当前阶段状态不允许此操作'
      return
    }
    const validatedRunId = validated.identity.runId
    clearReviewedStage(stageCacheKey(
      validatedRunId,
      validated.identity.track,
      validated.identity.stage,
    ))
    const result = await postMutation(
      `${endpoint(validated.view, validatedRunId)}/${action}`,
      stageExpectation(validated.identity),
    )
    if (!result) return
    const { response, body } = result
    if (!response.ok) actionError.value = errorMessage(body, '阶段操作失败')
    else await refreshRun()
  } catch {
    actionError.value = '阶段操作失败'
  } finally {
    endMutation()
  }
}

async function saveOverride(view: StageView) {
  if (mutating.value || activeRunId.value === null || view.track === null || view.stage === 'safety') return
  const expected = reviewedIdentityFor(view)
  if (!expected || expected.track === null || expected.stage === 'safety') {
    actionError.value = '当前阶段尚未成功加载并复核'
    return
  }
  const reason = overrideReason.value.trim()
  const code = overrideCode.value.trim()
  if (!code || !reason) {
    actionError.value = '代码和覆盖理由均为必填'
    return
  }
  if (!beginMutation()) return
  actionError.value = ''
  try {
    const validated = (await beforeMutation([expected]))?.[0]
    if (!validated || !actionsFor(validated.view).override) {
      actionError.value = '当前阶段不可人工覆盖'
      return
    }
    if (validated.identity.track === null || validated.identity.stage === 'safety') return
    const validatedRunId = validated.identity.runId
    const validatedTrack = validated.identity.track
    const validatedStage = validated.identity.stage
    clearReviewedStage(stageCacheKey(validatedRunId, validatedTrack, validatedStage))
    const result = await postMutation(`/api/discoveries/${validatedRunId}/overrides`, {
        track: validatedTrack,
        stage: validatedStage,
        attempt: validated.identity.attempt,
        ...stageExpectation(validated.identity),
        code,
        action: overrideAction.value,
        reason,
    })
    if (!result) return
    const { response, body } = result
    if (!response.ok) {
      actionError.value = errorMessage(body, '人工覆盖保存失败')
    } else {
      overrideCode.value = ''
      overrideReason.value = ''
      await refreshRun()
    }
  } catch {
    actionError.value = '人工覆盖保存失败'
  } finally {
    endMutation()
  }
}

async function abandonRun() {
  const current = state.value
  if (
    mutating.value
    || current.kind !== 'ready'
    || !canAbandonRun(current.run.current, current.run.status)
  ) return
  const expectedRunId = current.run.id
  const expectedWeekKey = current.run.week_key
  if (!beginMutation()) return
  actionError.value = ''
  try {
    const refreshed = await readCurrent()
    if (
      !refreshed
      || state.value.kind !== 'ready'
      || state.value.run.id !== expectedRunId
      || state.value.run.week_key !== expectedWeekKey
      || !canAbandonRun(state.value.run.current, state.value.run.status)
    ) {
      actionError.value = '当前批次或周次已变化，无法放弃旧批次'
      return
    }
    const validatedRunId = state.value.run.id
    const validatedWeekKey = state.value.run.week_key
    const result = await postMutation(`/api/discoveries/${validatedRunId}/abandon`, {
        expected_run_id: validatedRunId,
        expected_week_key: validatedWeekKey,
    })
    if (!result) return
    const { response, body } = result
    if (!response.ok) actionError.value = errorMessage(body, '无法放弃本批次')
    else await refreshRun()
  } catch {
    actionError.value = '无法放弃本批次'
  } finally {
    endMutation()
  }
}

async function onStageSelection(track: Track) {
  if (mutating.value) return
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

function trackShowsCandidateCases(track: Track, run: DiscoveryRun): boolean {
  const row = run.tracks?.[track]
  if (!row) return false
  if (row.stage === 'adjudication') return true
  return row.stage === 'competition' && row.status === 'confirmed'
}

function showsCandidateCases(view: StageView): boolean {
  if (view.track === null) return false
  if (view.stage === 'adjudication') return true
  return view.stage === 'competition' && view.status === 'confirmed'
}

function adjudicationCases(view: StageView, code?: string): AdjudicationCase[] {
  if (view.track === null || !showsCandidateCases(view)) return []
  const payload = adjudicationPayloads.value[view.track]
  if (!payload) return []
  return payload.pass
    .filter((row) => code === undefined || row.code === code)
    .map((row) => {
      const evidence = payload.evidence.filter(
        (item) => item.code === row.code
          && item.competition_stage_id === payload.current.id
          && item.competition_attempt === payload.current.attempt,
      )
      return {
        code: row.code,
        name: row.name,
        surfacedReason: candidateSurfacedReason(row, evidence),
        overridden: row.override?.action === 'forced_pass',
        track: view.track as Track,
        evidence,
        decision: payload.decisions.find(
          (item) => item.code === row.code
            && item.competition_stage_id === payload.current.id
            && item.competition_attempt === payload.current.attempt,
        ) ?? null,
        change: payload.changes.find((item) => item.code === row.code) ?? null,
      }
    })
}

function keeperCount(view: StageView): number {
  return adjudicationCases(view).filter((item) => item.decision?.decision === 'keep').length
}

function allCasesDecided(view: StageView): boolean {
  return view.track !== null
    && adjudicationPayloads.value[view.track] !== undefined
    && adjudicationCases(view).every((item) => item.decision !== null)
}

function canCompleteAdjudication(view: StageView): boolean {
  return allCasesDecided(view) && keeperRange(keeperCount(view)) !== 'over-limit'
}

function casesLoaded(view: StageView): boolean {
  return view.track !== null
    && candidatePayloadStates.value[view.track]?.kind === 'ready'
    && adjudicationPayloads.value[view.track] !== undefined
}

function candidatePayloadError(view: StageView): string {
  if (view.track === null) return ''
  const candidateState = candidatePayloadStates.value[view.track]
  return candidateState?.kind === 'error' ? candidateState.message : ''
}

function casesLoading(view: StageView): boolean {
  return view.track !== null
    && candidatePayloadStates.value[view.track]?.kind === 'loading'
}

function researchVerdict(item: AdjudicationCase): '值得深研' | '观察' | '排除' {
  if (item.decision?.decision === 'keep') return '值得深研'
  if (item.decision?.decision === 'exclude') return '排除'
  return '观察'
}

function draftKey(item: AdjudicationCase): string {
  return `${item.track}:${item.code}`
}

function exclusionReason(item: AdjudicationCase): string {
  return exclusionReasons.value[draftKey(item)] ?? ''
}

function factEvidence(item: AdjudicationCase): ParsedDiscoveryEvidence[] {
  return item.evidence.filter((evidence) => evidence.kind === 'fact')
}

function riskText(item: AdjudicationCase): string {
  const risk = item.evidence.find((evidence) => evidence.kind === 'risk')?.display_text
  return risk ? formatEvidenceText(risk) : '尚无结构化反方证据'
}

function caseRiskCount(item: AdjudicationCase): number {
  const risk = item.evidence.find((evidence) => evidence.kind === 'risk')
  if (!risk || risk.rule_id === `${item.track}.risk.none`) return 0
  return risk.display_text.split('；').filter(Boolean).length
}

function freshnessText(item: AdjudicationCase): string {
  const dates = [...new Set(item.evidence.map((evidence) => evidence.as_of).filter(Boolean))]
  const gaps = item.evidence.some((evidence) => evidence.data_completeness !== 'complete')
    ? '存在部分数据缺口'
    : '结构化证据完整'
  return `${gaps}；截至 ${dates.length ? dates.join('、') : '日期不可用'}`
}

function weeklyChangeText(view: StageView, item: AdjudicationCase): string {
  const change = weeklyChange(view, item)
  return change ? discoveryChangeLabels(change) : '无可比的上周记录（上周数据不完整或没有上周）'
}

function weeklyChange(
  view: StageView,
  item: AdjudicationCase,
): ParsedDiscoveryChange | null {
  const terminal = view.status === 'confirmed' || view.status === 'insufficient_candidates'
  return (
    terminal
      ? view.payload?.changes.find((candidate) => candidate.code === item.code)
      : item.change
  ) ?? null
}

function changeName(view: StageView, change: ParsedDiscoveryChange): string {
  return change.name || rows(view).find((row) => row.code === change.code)?.name || change.code
}

function weeklyStateText(
  change: ParsedDiscoveryChange | null,
  side: 'previous' | 'current',
): string {
  return change ? CHANGE_STATE_ZH[change[`${side}_state`]] : '无对比记录'
}

function weeklyReasonText(
  change: ParsedDiscoveryChange | null,
  side: 'previous' | 'current',
): string {
  const reason = change?.[`${side}_reason`]
  return reason ? formatEvidenceText(reason) : '无'
}

async function saveDecision(
  view: StageView,
  item: AdjudicationCase,
  decision: 'keep' | 'exclude',
) {
  if (mutating.value || view.track === null) return
  const expected = reviewedIdentityFor(view)
  if (!expected) {
    actionError.value = '当前裁决阶段尚未成功加载并复核'
    return
  }
  const reason = exclusionReason(item).trim()
  if (decision === 'exclude' && !reason) {
    actionError.value = '排除必须填写理由'
    return
  }
  if (!beginMutation()) return
  actionError.value = ''
  try {
    const validated = (await beforeMutation([expected]))?.[0]
    if (!validated || validated.view.status !== 'waiting_confirmation') {
      actionError.value = '当前裁决阶段不可编辑'
      return
    }
    const result = await postMutation(
      `/api/discoveries/${validated.identity.runId}/decisions`,
      {
        track: item.track,
        code: item.code,
        decision,
        reason: decision === 'exclude' ? reason : null,
        ...stageExpectation(validated.identity),
      },
    )
    if (!result) return
    if (!result.response.ok) {
      actionError.value = errorMessage(result.body, '裁决保存失败')
      return
    }
    if (decision === 'exclude') {
      exclusionReasons.value = {
        ...exclusionReasons.value,
        [draftKey(item)]: '',
      }
    }
    await refreshRun()
  } catch {
    actionError.value = '裁决保存失败'
  } finally {
    endMutation()
  }
}

async function completeAdjudication(view: StageView) {
  if (mutating.value || view.track === null || !canCompleteAdjudication(view)) return
  const expected = reviewedIdentityFor(view)
  if (!expected) {
    actionError.value = '当前裁决阶段尚未成功加载并复核'
    return
  }
  if (!beginMutation()) return
  actionError.value = ''
  try {
    const validated = (await beforeMutation([expected]))?.[0]
    if (!validated || validated.view.status !== 'waiting_confirmation') {
      actionError.value = '当前裁决阶段不可完成'
      return
    }
    const result = await postMutation(
      `${endpoint(validated.view, validated.identity.runId)}/confirm`,
      stageExpectation(validated.identity),
    )
    if (!result) return
    if (!result.response.ok) actionError.value = errorMessage(result.body, '裁决完成失败')
    else await refreshRun()
  } catch {
    actionError.value = '裁决完成失败'
  } finally {
    endMutation()
  }
}

function canResearchPass(
  view: StageView,
  group: 'pass' | 'drop' | 'insufficient',
  row: DiscoveryRow,
): boolean {
  return view.track !== null
    && view.stage === 'competition'
    && view.status === 'confirmed'
    && group === 'pass'
    && row.verdict === 'pass'
}

async function openDiscoveryResearch(track: string, code: string) {
  const current = state.value
  if (researchingCode.value || current.kind !== 'ready') return
  researchingCode.value = code
  actionError.value = ''
  researchErrorCode.value = ''
  try {
    const response = await fetch(`/api/discoveries/${current.run.id}/research`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track, code }),
    })
    const data: unknown = await response.json()
    if (!response.ok) {
      actionError.value = errorMessage(data, '无法加入本周深研')
      researchErrorCode.value = code
      return
    }
    if (!isRecord(data) || nonNegativeInteger(data.id) === null || data.id === 0) {
      actionError.value = '服务返回的研究任务无效'
      return
    }
    await router.push(`/research/${data.id}`)
  } catch {
    actionError.value = '无法加入本周深研'
  } finally {
    researchingCode.value = ''
  }
}

async function startResearch(item: AdjudicationCase) {
  if (item.decision?.decision !== 'keep') return
  await openDiscoveryResearch(item.track, item.code)
}

async function startTrackResearch(view: StageView, row: DiscoveryRow) {
  if (!canResearchPass(view, 'pass', row) || !view.track) return
  await openDiscoveryResearch(view.track, row.code)
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

function shortVersion(value: string | null | undefined): string {
  if (!value) return '不可用'
  const parts = value.split(':')
  if (parts.length >= 2) {
    const head = parts[0]
    const tail = parts[parts.length - 1]
    const shortHash = tail && tail.length > 8 ? tail.slice(0, 6) : tail
    return shortHash ? `${head} (${shortHash})` : head || value
  }
  return value.length > 24 ? `${value.slice(0, 20)}…` : value
}

function markerPosition(marker: ThresholdMarker): string {
  if (marker.status !== 'available' || marker.position === null) return '不可用'
  const position = Number(marker.position)
  return Number.isFinite(position) ? `${position}%` : '不可用'
}

const fillBlocksStart = computed(() => {
  const fill = fillStatus.value
  if (fill === null) return false
  return !(fill.status === 'done' && fill.snapshotStatus === 'ready' && fill.total > 0)
})

const fillProgress = computed(() => {
  const fill = fillStatus.value
  if (fill === null) return ''
  if (fill.total <= 0) {
    return fill.status === 'running' || fill.status === 'paused'
      ? '正在准备全市场名单。'
      : ''
  }
  const label = fill.status === 'running'
    ? '正在补数'
    : fill.status === 'paused'
      ? '补数已暂停'
      : fill.status === 'done'
        ? '快照已完成'
        : '补数状态'
  const reason = fill.status === 'paused' && fill.lastError ? `原因：${fill.lastError}。` : ''
  const asOf = fill.dataAsOf ? `现有数据截至 ${fill.dataAsOf}。` : ''
  return `${label}：完成 ${fill.completed} / ${fill.total}，失败 ${fill.failed}，待处理 ${fill.pending}。${reason}${asOf}`
})

const fillButtonLabel = computed(() => {
  const status = fillStatus.value?.status
  if (fillStarting.value || status === 'running') return '正在补数…'
  return status === 'paused' ? '继续补数' : '更新全市场数据'
})

async function startFill() {
  if (fillStarting.value) return
  fillStarting.value = true
  actionError.value = ''
  try {
    const response = await fetch('/api/discovery-data/fill', { method: 'POST' })
    if (!response.ok) {
      const body: unknown = await response.json().catch(() => null)
      actionError.value = errorMessage(body, '无法开始补数')
    }
  } catch {
    actionError.value = '无法开始补数'
  } finally {
    fillStarting.value = false
    watchFill()
  }
}

async function loadFillStatus() {
  try {
    const response = await fetch('/api/discovery-data/status')
    if (!response.ok) return
    const body: unknown = await response.json()
    if (!isRecord(body) || typeof body.status !== 'string') return
    const total = nonNegativeInteger(body.total)
    const completed = nonNegativeInteger(body.completed)
    const failed = nonNegativeInteger(body.failed)
    const pending = nonNegativeInteger(body.pending)
    if (total === null || completed === null || failed === null || pending === null) return
    const sourceAsOf = isRecord(body.readiness) && isRecord(body.readiness.source_as_of)
      ? body.readiness.source_as_of
      : null
    fillStatus.value = {
      status: body.status,
      snapshotStatus: typeof body.snapshot_status === 'string' ? body.snapshot_status : null,
      total,
      completed,
      failed,
      pending,
      lastError: typeof body.last_error === 'string' ? body.last_error : null,
      dataAsOf: typeof sourceAsOf?.security === 'string' ? sourceAsOf.security : null,
    }
  } catch {
    // Progress is context for the empty state; a missed poll can retry.
  }
}

function watchFill() {
  void loadFillStatus()
  if (fillTimer !== null) return
  fillTimer = setInterval(() => {
    if (!mounted || state.value.kind !== 'empty') return
    void loadFillStatus()
  }, 8000)
}

onMounted(() => {
  mounted = true
  void loadCurrent()
})

onBeforeUnmount(() => {
  mounted = false
  currentRequestSequence += 1
  mutationRequestSequence += 1
  currentController?.abort()
  currentController = null
  mutationController?.abort()
  mutationController = null
  abortStageRequests()
  abortAdjudicationRequests()
  activeRunId.value = null
  stopPoll()
  stopCurrentRefresh()
  if (fillTimer !== null) {
    clearInterval(fillTimer)
    fillTimer = null
  }
})
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>每周发现</h1>
      <p class="muted">按固定尺子逐层筛选；下游重算时，已完成的轨道筛选仍可查看与评估。</p>
    </div>

    <p role="status" aria-live="polite" aria-atomic="true">{{ announcement }}</p>
    <p class="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {{ transitionAnnouncement }}
    </p>

    <div v-if="state.kind === 'empty'" class="panel">
      <h2>本周尚未创建批次</h2>
      <p v-if="fillBlocksStart" role="alert">全市场数据快照尚未完成，还不能开始本周发现。</p>
      <p v-else class="muted">创建后将从共同排雷开始。</p>
      <p v-if="fillProgress" class="muted">{{ fillProgress }}</p>
      <p v-if="actionError" role="alert">{{ actionError }}</p>
      <button
        type="button"
        :disabled="fillStarting || fillStatus?.status === 'running'"
        @click="startFill"
      >
        {{ fillButtonLabel }}
      </button>
      <button type="button" :disabled="starting || fillBlocksStart" @click="startDiscovery">
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
        <div class="toolbar">
          <h2>本周批次</h2>
          <span class="badge badge-info">{{ state.run.week_key }}</span>
          <span
            class="badge"
            :class="state.run.status === 'completed' ? 'badge-pass' : state.run.status === 'failed' ? 'badge-fail' : 'badge-warn'"
          >
            状态：{{ STATUS_ZH[state.run.status] }}
          </span>
          <button type="button" :disabled="mutating" @click="readCurrent()">刷新状态</button>
        </div>
        <div class="run-meta-bar">
          <span>快照时间：<strong>{{ formatTimestamp(state.run.snapshot_at) }}</strong></span>
          <span :title="state.run.data_version || '不可用'">
            数据版本：<code>{{ shortVersion(state.run.data_version) }}</code>
          </span>
          <span :title="state.run.financial_version || '不可用'">
            财务版本：<code>{{ shortVersion(state.run.financial_version) }}</code>
          </span>
          <span :title="state.run.valuation_version || '不可用'">
            估值版本：<code>{{ shortVersion(state.run.valuation_version) }}</code>
          </span>
        </div>
        <p v-if="state.run.error" role="alert">{{ state.run.error }}</p>
      </div>

      <p v-if="actionError" role="alert">{{ actionError }}</p>

      <div class="stage-tabs" role="tablist" aria-label="发现层级切换">
        <button
          v-for="tabView in stageViews"
          :key="'tab-' + tabView.key"
          type="button"
          class="stage-tab"
          :class="{ active: activeStageFilter === (tabView.track ?? 'safety') }"
          @click="activeStageFilter = tabView.track ?? 'safety'"
        >
          <span>{{ tabView.track ? TRACK_ZH[tabView.track] : '共同排雷' }}</span>
          <span v-if="tabView.payload" class="stage-tab-count">
            {{ tabView.payload.summary.counts.pass }}
          </span>
        </button>
        <button
          type="button"
          class="stage-tab"
          :class="{ active: activeStageFilter === 'all' }"
          @click="activeStageFilter = 'all'"
        >
          全部展开
        </button>
      </div>

      <article
        v-for="view in stageViews"
        v-show="activeStageFilter === 'all' || activeStageFilter === (view.track ?? 'safety')"
        :key="view.key"
        class="panel stage-panel"
        :aria-busy="mutating
          || !!researchingCode
          || (view.stage === 'adjudication' && casesLoading(view))
          || (view.created && (view.status === 'queued' || view.status === 'running'))"
        :aria-labelledby="`discovery-stage-${view.key.replace(':', '-')}`"
      >
        <div class="toolbar">
          <h2 :id="`discovery-stage-${view.key.replace(':', '-')}`">{{ view.title }}</h2>
          <span
            class="badge"
            :class="view.status === 'confirmed' ? 'badge-pass' : view.status === 'failed' || view.status === 'data_not_ready' || view.status === 'ruler_not_ready' ? 'badge-fail' : 'badge-warn'"
          >
            状态：{{ STAGE_STATUS_ZH[view.status] }}
          </span>
          <label v-if="view.track" class="stage-selector">
            查看阶段
            <select
              v-model="selectedTrackStages[view.track]"
              @change="onStageSelection(view.track)"
            >
              <option
                v-for="stage in VISIBLE_TRACK_STAGES"
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
        <p v-else-if="upstreamComputing(view)" class="notice">
          后续阶段仍在计算。最终候选出来之前，不能从本层进入深研。
        </p>
        <p v-else-if="view.status === 'queued' || view.status === 'running'" class="muted">
          正在生成本层结果，完成后会自动停止刷新。
          <template v-if="view.track && view.stage === 'competition'">
            可先
            <button type="button" @click="selectTrackStage(view.track, 'track_gate')">
              查看轨道筛选
            </button>
            通过名单并评估。
          </template>
        </p>
        <p v-else-if="view.status === 'data_not_ready' || view.status === 'ruler_not_ready'">
          本层只读且不可重试；请放弃本批次并重新创建。
        </p>
        <p v-else-if="view.status === 'failed'" class="muted">
          仅瞬时失败可重试；永久失败请放弃后重新创建。
        </p>

        <section v-if="view.payload?.changes.length" class="weekly-changes">
          <h3>本周变化</h3>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>名称（代码）</th>
                  <th>变化分类</th>
                  <th>上周状态 / 理由</th>
                  <th>本周状态 / 理由</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="change in view.payload.changes" :key="change.code">
                  <td>{{ changeName(view, change) }}（{{ change.code }}）</td>
                  <td>{{ discoveryChangeLabels(change) }}</td>
                  <td>
                    {{ weeklyStateText(change, 'previous') }} /
                    {{ weeklyReasonText(change, 'previous') }}
                  </td>
                  <td>
                    {{ weeklyStateText(change, 'current') }} /
                    {{ weeklyReasonText(change, 'current') }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <template v-if="view.payload && view.stage !== 'adjudication'">
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
              <p class="count-strip">
                <span class="count-chip">输入 <strong>{{ view.payload.summary.counts.input }}</strong></span>
                <span class="count-chip count-pass">通过 <strong>{{ view.payload.summary.counts.pass }}</strong></span>
                <span class="count-chip count-drop">淘汰 <strong>{{ view.payload.summary.counts.drop }}</strong></span>
                <span class="count-chip count-insuf">数据不足 <strong>{{ view.payload.summary.counts.insufficient }}</strong></span>
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
            class="table-wrap result-group-wrap"
            tabindex="0"
            :aria-label="stageAriaLabel(view.title, `${group.title}，可横向滚动`)"
          >
            <button
              v-if="groupCollapsed(view, group.key)"
              type="button"
              class="expand-btn"
              :class="`expand-${group.key}`"
              @click="showGroup(view, group.key)"
            >
              展开{{ group.title }}（{{ resultRows(view, group.key).length }}）
            </button>
            <table v-else>
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
                <template v-for="row in resultRows(view, group.key)" :key="row.code">
                  <tr>
                    <td>
                      {{ row.name }}
                      <button
                        v-if="canResearchPass(view, group.key, row)"
                        type="button"
                        :disabled="!!researchingCode"
                        :aria-label="`评估 ${row.name}，继续深度研究`"
                        @click="startTrackResearch(view, row)"
                      >
                        {{ researchingCode === row.code ? '正在创建深研…' : '评估' }}
                      </button>
                      <p
                        v-if="researchErrorCode === row.code && actionError"
                        role="alert"
                      >
                        {{ actionError }}
                      </p>
                    </td>
                    <td>{{ row.code }}</td>
                    <td>{{ verdictLabel(row.original_verdict) }}</td>
                    <td>{{ verdictLabel(row.verdict) }}</td>
                    <td>{{ row.reason ? formatEvidenceText(row.reason) : '—' }}</td>
                    <td>{{ row.rank ?? '—' }}</td>
                    <td>{{ row.metrics_unavailable_reason || '可用' }}</td>
                    <td v-for="metric in view.metricKeys" :key="metric">
                      {{ formatValue(row.metrics[metric]) }}
                    </td>
                  </tr>
                  <template v-if="group.key === 'pass' && showsCandidateCases(view) && view.track">
                    <tr
                      v-for="caseItem in adjudicationCases(view, row.code)"
                      :key="`${caseItem.track}:${caseItem.code}`"
                      class="case-row"
                    >
                      <td :colspan="7 + view.metricKeys.length">
                        <details class="case-details">
                          <summary>
                            候选案例 · {{ caseItem.name }}（{{ caseItem.code }}）· {{ researchVerdict(caseItem) }}
                            <span
                              class="badge"
                              :class="caseRiskCount(caseItem) > 0 ? 'badge-warn' : 'badge-pass'"
                            >
                              {{ caseRiskCount(caseItem) > 0 ? `${caseRiskCount(caseItem)} 项风险提示` : '无反方风险' }}
                            </span>
                          </summary>
                          <article
                            class="case-card"
                            :aria-labelledby="`case-${caseItem.track}-${caseItem.code}`"
                          >
                            <div class="toolbar">
                              <h3 :id="`case-${caseItem.track}-${caseItem.code}`">
                                {{ caseItem.name }}（{{ caseItem.code }}）
                              </h3>
                              <strong class="research-verdict">{{ researchVerdict(caseItem) }}</strong>
                            </div>
                            <dl>
                              <div><dt>轨道</dt><dd>{{ TRACK_ZH[caseItem.track] }}</dd></div>
                              <div v-if="caseItem.overridden">
                                <dt>入围原因</dt><dd>{{ caseItem.surfacedReason }}</dd>
                              </div>
                              <div>
                                <dt>关键证据</dt>
                                <dd>
                                  <ul>
                                    <li v-for="evidence in factEvidence(caseItem)" :key="evidence.id">
                                      {{ formatEvidenceText(evidence.display_text) }}
                                    </li>
                                  </ul>
                                </dd>
                              </div>
                              <div><dt>风险与反方证据</dt><dd>{{ riskText(caseItem) }}</dd></div>
                              <div><dt>数据缺口与新鲜度</dt><dd>{{ freshnessText(caseItem) }}</dd></div>
                              <div><dt>变化分类</dt><dd>{{ weeklyChangeText(view, caseItem) }}</dd></div>
                              <div>
                                <dt>上周状态</dt>
                                <dd>{{ weeklyStateText(weeklyChange(view, caseItem), 'previous') }}</dd>
                              </div>
                              <div>
                                <dt>本周状态</dt>
                                <dd>{{ weeklyStateText(weeklyChange(view, caseItem), 'current') }}</dd>
                              </div>
                              <div>
                                <dt>上周理由</dt>
                                <dd>{{ weeklyReasonText(weeklyChange(view, caseItem), 'previous') }}</dd>
                              </div>
                              <div>
                                <dt>本周理由</dt>
                                <dd>{{ weeklyReasonText(weeklyChange(view, caseItem), 'current') }}</dd>
                              </div>
                            </dl>
                            <button
                              v-if="caseItem.decision?.decision === 'keep'"
                              type="button"
                              :disabled="!!researchingCode"
                              @click="startResearch(caseItem)"
                            >
                              {{ researchingCode === caseItem.code ? '正在创建深研…' : '加入本周深研' }}
                            </button>
                          </article>
                        </details>
                      </td>
                    </tr>
                  </template>
                </template>
                <tr v-if="!resultRows(view, group.key).length">
                  <td :colspan="7 + view.metricKeys.length">暂无{{ group.title }}</td>
                </tr>
              </tbody>
            </table>
            <template v-if="group.key === 'pass' && showsCandidateCases(view) && view.track">
              <p v-if="casesLoading(view)" class="muted">
                正在读取候选案例…
              </p>
              <div v-if="candidatePayloadError(view)" role="alert">
                <p>{{ candidatePayloadError(view) }}</p>
                <button type="button" @click="retryAdjudicationPayload(view)">
                  重试候选案例
                </button>
              </div>
              <p v-else-if="casesLoaded(view) && !adjudicationCases(view).length" class="muted">
                本轨道没有案例卡。
              </p>
            </template>
          </div>

        </template>

        <div class="stage-actions">
          <button
            v-if="actionsFor(view).retry"
            type="button"
            :disabled="mutating"
            @click="stageMutation(view, 'retry')"
          >
            重试本层
          </button>
        </div>
      </article>

      <div
        v-if="canAbandonRun(state.run.current, state.run.status)"
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

.run-meta-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1.25rem;
  font-size: 0.85rem;
  color: #464139;
  background: #faf8f3;
  border: 1px solid #e8e2d5;
  padding: 0.45rem 0.75rem;
}

.run-meta-bar code {
  font-size: 0.8rem;
  background: #efeae0;
  padding: 0.05rem 0.35rem;
  border-radius: 2px;
  color: #2c2925;
}

.stage-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin: 0.65rem 0;
}

.stage-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.36rem 0.78rem;
  border-radius: 3px;
  border: 1px solid #d0c8b8;
  background: #fff;
  color: #3b3730;
  font-size: 0.9rem;
}

.stage-tab-count {
  font-variant-numeric: tabular-nums;
  font-size: 0.78rem;
  background: #f1ece2;
  color: #5c564c;
  padding: 0.05rem 0.38rem;
  border-radius: 2px;
}

.stage-tab.active {
  background: #1b5e4a;
  border-color: #1b5e4a;
  color: #fff;
  font-weight: 600;
}

.stage-tab.active .stage-tab-count {
  background: rgba(255, 255, 255, 0.2);
  color: #fff;
}

.stage-panel h2,
.stage-panel h3 {
  margin-top: 0;
}

.stage-panel h3 {
  font-size: 0.92rem;
  color: #464139;
  margin-bottom: 0.4rem;
}

.stage-selector {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.88rem;
}

.stage-summary {
  display: grid;
  grid-template-columns: 1.4fr 0.9fr 1.1fr;
  gap: 0.65rem;
}

.stage-summary > div,
.summary-details {
  border: 1px solid #e4ddd0;
  background: #faf8f3;
  padding: 0.65rem 0.85rem;
}

.stage-summary p,
.stage-summary ul {
  margin: 0;
  font-size: 0.88rem;
}

.stage-summary ul {
  padding-left: 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.count-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.count-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  background: #fff;
  border: 1px solid #ddd5c5;
  padding: 0.2rem 0.5rem;
  border-radius: 2px;
  font-size: 0.84rem;
  font-variant-numeric: tabular-nums;
}

.count-pass {
  background: #e3f1ec;
  border-color: #b7dccf;
  color: #1b5e4a;
}

.count-drop {
  background: #fbeaea;
  border-color: #e6b8b8;
  color: #9f1d1d;
}

.count-insuf {
  background: #fdf3e6;
  border-color: #ead0ad;
  color: #8a4b08;
}

.result-group-wrap {
  display: inline-block;
  margin-right: 0.5rem;
}

.result-group-wrap:has(table) {
  display: block;
  margin-right: 0;
}

.expand-btn {
  font-weight: 600;
}

.expand-pass {
  border-color: #b7dccf;
  background: #f2f9f6;
  color: #1b5e4a;
}

.expand-drop {
  border-color: #e2dad0;
  background: #faf8f3;
  color: #5c564c;
}

.expand-insufficient {
  border-color: #ead0ad;
  background: #fdfaf4;
  color: #8a4b08;
}

.summary-details {
  margin: 0.65rem 0;
}

.summary-details summary {
  cursor: pointer;
  font-weight: 600;
  font-size: 0.88rem;
}

.summary-details h3 {
  margin-top: 0.75rem;
}

.table-wrap {
  overflow-x: auto;
  margin: 0.45rem 0 0.75rem;
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
  margin-top: 0.5rem;
}

.case-row td {
  white-space: normal;
}

.case-details {
  position: sticky;
  left: 0;
  max-width: min(58rem, calc(100vw - 4rem));
}

.case-details > summary {
  cursor: pointer;
  font-weight: 600;
}

.case-card {
  border: 1px solid #d8d2c4;
  border-left: 3px solid #1b5e4a;
  background: #faf8f3;
  padding: 0.85rem 1rem;
  margin: 0.65rem 0;
}

.case-card dl,
.case-card dd,
.case-card ul {
  margin: 0;
}

.case-card dl > div {
  display: grid;
  grid-template-columns: 10rem 1fr;
  gap: 0.75rem;
  padding: 0.4rem 0;
  border-top: 1px solid #eee8dd;
}

.case-card dt,
.research-verdict {
  font-weight: 600;
}

.case-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 0.6rem;
  margin-top: 0.75rem;
}

.candidate-warning {
  color: #8a4b08;
  font-weight: 600;
}

.candidate-error {
  color: #9f1d1d;
  font-weight: 600;
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

  .case-card dl > div {
    grid-template-columns: 1fr;
    gap: 0.2rem;
  }
}
</style>
