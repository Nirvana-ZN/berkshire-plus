export type DiscoveryTrack = 'quality_discount' | 'deep_value' | 'garp'
export type DiscoveryTrackStage = 'track_gate' | 'competition' | 'adjudication'
export type DiscoveryStageName = 'safety' | DiscoveryTrackStage
export type DiscoveryStageStatus =
  | 'queued'
  | 'running'
  | 'waiting_confirmation'
  | 'confirmed'
  | 'failed'
  | 'data_not_ready'
  | 'ruler_not_ready'
  | 'insufficient_candidates'
export type DiscoveryFailureKind = 'transient' | 'permanent' | null
export type MetricValue = string | number | boolean | null | MetricValue[]

export interface ParsedDiscoveryOverride {
  id: number
  track: DiscoveryTrack
  stage: DiscoveryTrackStage
  attempt: number
  code: string
  action: 'forced_pass' | 'forced_drop'
  reason: string
}

export interface ParsedDiscoveryRow {
  code: string
  name: string
  verdict: 'pass' | 'drop' | 'insufficient'
  original_verdict: 'pass' | 'drop' | 'insufficient'
  reason: string | null
  rank: number | null
  metrics: Record<string, MetricValue>
  metrics_unavailable_reason: string | null
  override: ParsedDiscoveryOverride | null
}

export interface ParsedDiscoveryDecision {
  id: number
  track: DiscoveryTrack
  competition_stage_id: number
  competition_attempt: number
  code: string
  decision: 'keep' | 'exclude'
  exclusion_reason: string | null
  keep_note: string | null
}

export interface ParsedDiscoveryEvidence {
  id: number
  track: DiscoveryTrack
  competition_stage_id: number
  competition_attempt: number
  code: string
  ruler_version_id: number
  rule_version: 'deterministic-evidence-v2'
  kind: 'fact' | 'risk'
  rule_id: string
  input_fields: Record<string, unknown>
  display_text: string
  as_of: string
  data_completeness: 'complete' | 'partial'
}

export interface ParsedDiscoveryChange {
  code: string
  classification: DiscoveryChangeClassification
  classifications: DiscoveryChangeClassification[]
  membership: DiscoveryMembership
  cause: DiscoveryChangeCause
  reason_changed: boolean
  previous_state: DiscoveryResultState
  current_state: DiscoveryResultState
  previous_reason: string | null
  current_reason: string | null
}

type DiscoveryMembership = 'new' | 'dropped' | 'reentered' | 'continuous'
type DiscoveryChangeCause =
  | 'data_changed'
  | 'rule_changed'
  | 'both_changed'
  | 'decision_changed'
  | null
type DiscoveryChangeClassification =
  | DiscoveryMembership
  | Exclude<DiscoveryChangeCause, null>
  | 'reason_changed'
type DiscoveryResultState = 'absent' | 'pass' | 'drop' | 'insufficient'

interface ParsedDistribution {
  status: 'available' | 'unavailable'
  reason?: string
  count: number
  min: string | null
  p25: string | null
  median: string | null
  p75: string | null
  max: string | null
}

export interface ParsedStageResponse {
  current: {
    id: number
    track: DiscoveryTrack | null
    stage: DiscoveryStageName
    attempt: number
    status: DiscoveryStageStatus
    error: string | null
    failure_kind: DiscoveryFailureKind
    input_fingerprint: string
    ruler_version_id: number | null
    stage_revision: number
    mutation_version: string
  }
  pass: ParsedDiscoveryRow[]
  drop: ParsedDiscoveryRow[]
  insufficient: ParsedDiscoveryRow[]
  overrides: ParsedDiscoveryOverride[]
  decisions: ParsedDiscoveryDecision[]
  evidence: ParsedDiscoveryEvidence[]
  changes: ParsedDiscoveryChange[]
  summary: {
    schema_version: string
    rules: { id: string; text: string }[]
    ruler: {
      version_id: number | null
      reason: string
      scope: string
      algorithm_version: string | null
      status: string
    }
    counts: {
      input: number
      pass: number
      drop: number
      insufficient: number
      overrides: number
    }
    distributions: Record<string, ParsedDistribution>
    threshold_markers: {
      id: string
      threshold: string
      metric: string | null
      direction: 'gt' | 'gte' | 'lte' | null
      position: string | null
      status: 'available' | 'unavailable' | 'unresolved'
    }[]
  }
}

const STAGE_STATUSES: readonly DiscoveryStageStatus[] = [
  'queued',
  'running',
  'waiting_confirmation',
  'confirmed',
  'failed',
  'data_not_ready',
  'ruler_not_ready',
  'insufficient_candidates',
]
const TRACKS: readonly DiscoveryTrack[] = ['quality_discount', 'deep_value', 'garp']
const TRACK_STAGES: readonly DiscoveryTrackStage[] = [
  'track_gate',
  'competition',
  'adjudication',
]
const CHANGE_MEMBERSHIPS: readonly DiscoveryMembership[] = [
  'new',
  'dropped',
  'reentered',
  'continuous',
]
const CHANGE_CAUSES: readonly Exclude<DiscoveryChangeCause, null>[] = [
  'data_changed',
  'rule_changed',
  'both_changed',
  'decision_changed',
]
const EVIDENCE_RULE_VERSION = 'deterministic-evidence-v2'
const FALLBACK_RISK_TEXT = '尚无结构化反方证据'
const CHANGE_REASON_MAX_LENGTH = 2_000
const RISK_CATEGORIES = new Set([
  'metric_near_threshold',
  'worsening_trend',
  'sector_specific_risk',
  'missing_non_gating_evidence',
])
// Mirrors SUMMARY_* limits in discovery/summaries.py; 128 is the ruler's
// documented observation ceiling (for example MAX_FINANCIAL_ROWS).
export const RESULT_MAX_ROWS = 20_000
export const RESULT_METRICS_MAX_UTF8_BYTES = 64 * 1024
export const RESULT_METRIC_KEYS_MAX = 256
export const RESULT_OBSERVATIONS_MAX = 128
export const RESULT_METRIC_MAX_DEPTH = 128
export const RESULT_METRIC_MAX_NODES = RESULT_METRICS_MAX_UTF8_BYTES
export const STAGE_POLL_INTERVAL_MS = 800
export const STAGE_FETCH_TIMEOUT_MS = 5_000
export const CURRENT_FETCH_TIMEOUT_MS = 5_000
const INVALID_METRIC = Symbol('invalid-metric')

export interface StageReviewIdentity {
  runId: number
  track: DiscoveryTrack | null
  stage: DiscoveryStageName
  stageId: number
  attempt: number
  status: DiscoveryStageStatus
  inputFingerprint: string
  rulerVersionId: number | null
  stageRevision: number
  mutationVersion: string
  fingerprint: string
}

export function stageCacheKey(
  runId: number,
  track: DiscoveryTrack | null,
  stage: DiscoveryStageName,
): string {
  return `${runId}:${track ?? 'common'}:${stage}`
}

export function keeperRange(
  count: number,
): 'insufficient' | 'valid' | 'over-limit' {
  if (count > 5) return 'over-limit'
  return count >= 3 ? 'valid' : 'insufficient'
}

export function candidateSurfacedReason(
  row: ParsedDiscoveryRow,
  evidence: readonly ParsedDiscoveryEvidence[],
): string {
  if (row.override?.action === 'forced_pass') {
    const original = {
      pass: '入围',
      drop: '未入围',
      insufficient: '数据不足',
    }[row.original_verdict]
    return `人工覆盖改为入围：${row.override.reason}（原判定：${original}）`
  }
  return evidence
    .filter((item) => item.code === row.code && item.kind === 'fact')
    .slice(0, 5)
    .map((item) => item.display_text)
    .join('；')
}

export function shouldPollStage(created: boolean, status: DiscoveryStageStatus): boolean {
  return created && (status === 'queued' || status === 'running')
}

export function latestStagesNeedPolling(
  pointers: readonly (
    { created: boolean; status: DiscoveryStageStatus | null } | null | undefined
  )[],
): boolean {
  return pointers.some(
    (pointer) => pointer?.status != null
      && shouldPollStage(pointer.created, pointer.status),
  )
}

export function collectMetricKeys(
  rows: readonly { metrics: Readonly<Record<string, MetricValue>> }[],
): string[] {
  const keys = new Set<string>()
  for (const row of rows) {
    for (const key of Object.keys(row.metrics)) keys.add(key)
  }
  return [...keys].sort()
}

export function armFetchTimeout(
  controller: AbortController,
  timeoutMs: number,
  onTimeout: () => void = () => {},
  schedule: (callback: () => void, delay: number) => unknown =
    (callback, delay) => globalThis.setTimeout(callback, delay),
  cancel: (handle: unknown) => void =
    (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
): () => void {
  const handle = schedule(() => {
    onTimeout()
    controller.abort()
  }, timeoutMs)
  return () => cancel(handle)
}

export function armStageFetchTimeout(
  controller: AbortController,
  onTimeout: () => void = () => {},
  schedule?: (callback: () => void, delay: number) => unknown,
  cancel?: (handle: unknown) => void,
): () => void {
  return armFetchTimeout(
    controller,
    STAGE_FETCH_TIMEOUT_MS,
    onTimeout,
    schedule,
    cancel,
  )
}

export function stagePayloadFingerprint(value: unknown): string {
  const serialized = JSON.stringify(value) ?? 'undefined'
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < serialized.length; index += 1) {
    const code = serialized.charCodeAt(index)
    first = Math.imul(first ^ code, 0x01000193)
    second = Math.imul(second ^ code, 0x85ebca6b)
  }
  return `${serialized.length}:${(first >>> 0).toString(16)}:${(second >>> 0).toString(16)}`
}

export function sameStageReview(
  expected: StageReviewIdentity | null | undefined,
  actual: StageReviewIdentity | null | undefined,
): boolean {
  return expected != null
    && actual != null
    && expected.runId === actual.runId
    && expected.track === actual.track
    && expected.stage === actual.stage
    && expected.stageId === actual.stageId
    && expected.attempt === actual.attempt
    && expected.status === actual.status
    && expected.inputFingerprint === actual.inputFingerprint
    && expected.rulerVersionId === actual.rulerVersionId
    && expected.stageRevision === actual.stageRevision
    && expected.mutationVersion === actual.mutationVersion
    && expected.fingerprint === actual.fingerprint
}

export function matchesStagePointer(
  pointerAttempt: number | undefined,
  pointerStatus: DiscoveryStageStatus | null | undefined,
  payloadAttempt: number,
  payloadStatus: DiscoveryStageStatus,
): boolean {
  return (
    (pointerAttempt === undefined || pointerAttempt === payloadAttempt)
    && (pointerStatus == null || pointerStatus === payloadStatus)
  )
}

export function stageActions(
  stage: DiscoveryStageName,
  status: DiscoveryStageStatus,
  failureKind: DiscoveryFailureKind,
): { confirm: boolean; override: boolean; retry: boolean } {
  const waiting = status === 'waiting_confirmation' && stage !== 'adjudication'
  return {
    confirm: waiting,
    override: waiting && stage !== 'safety',
    retry: stage !== 'adjudication' && status === 'failed' && failureKind === 'transient',
  }
}

export function reviewedStageActions(
  reviewed: boolean,
  stage: DiscoveryStageName,
  status: DiscoveryStageStatus,
  failureKind: DiscoveryFailureKind,
): { confirm: boolean; override: boolean; retry: boolean } {
  return reviewed
    ? stageActions(stage, status, failureKind)
    : { confirm: false, override: false, retry: false }
}

export function canAbandonRun(current: boolean, status: string): boolean {
  return current && status !== 'abandoned'
}

export function stageAriaLabel(title: string, section: string): string {
  return `${title}：${section}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isStageStatus(value: unknown): value is DiscoveryStageStatus {
  return STAGE_STATUSES.some((status) => status === value)
}

function isTrack(value: unknown): value is DiscoveryTrack {
  return TRACKS.some((track) => track === value)
}

function isTrackStage(value: unknown): value is DiscoveryTrackStage {
  return TRACK_STAGES.some((stage) => stage === value)
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

function parseMetricValue(
  value: unknown,
  depth: number,
  budget: { nodes: number },
): MetricValue | typeof INVALID_METRIC {
  budget.nodes += 1
  if (budget.nodes > RESULT_METRIC_MAX_NODES) return INVALID_METRIC
  if (value === null) return null
  if (typeof value === 'string') return value
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : INVALID_METRIC
  }
  if (typeof value === 'boolean') return value
  if (!Array.isArray(value) || depth >= RESULT_METRIC_MAX_DEPTH) return INVALID_METRIC
  if (value.length > RESULT_OBSERVATIONS_MAX) return INVALID_METRIC
  const parsed: MetricValue[] = []
  for (const item of value) {
    const child = parseMetricValue(item, depth + 1, budget)
    if (child === INVALID_METRIC) return INVALID_METRIC
    parsed.push(child)
  }
  return parsed
}

export function parseMetricRecord(value: unknown): Record<string, MetricValue> | null {
  if (!isRecord(value)) return null
  const entries = Object.entries(value)
  if (entries.length > RESULT_METRIC_KEYS_MAX) return null
  try {
    if (
      new TextEncoder().encode(JSON.stringify(value)).byteLength
      > RESULT_METRICS_MAX_UTF8_BYTES
    ) return null
  } catch {
    return null
  }
  const budget = { nodes: 0 }
  const parsed: Record<string, MetricValue> = {}
  for (const [key, raw] of entries) {
    const metric = parseMetricValue(raw, 0, budget)
    if (metric === INVALID_METRIC) return null
    parsed[key] = metric
  }
  return parsed
}

export function renderMetricValue(value: MetricValue): string {
  if (value === null) return '不可用'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '不可用'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'string') return value || '不可用'
  return `[${value.map(renderMetricValue).join('、')}]`
}

function parseOverride(value: unknown): ParsedDiscoveryOverride | null {
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

function parseRow(value: unknown): ParsedDiscoveryRow | null {
  if (
    !isRecord(value)
    || typeof value.code !== 'string'
    || typeof value.name !== 'string'
    || !value.name
    || (value.verdict !== 'pass' && value.verdict !== 'drop' && value.verdict !== 'insufficient')
    || (
      value.original_verdict !== 'pass'
      && value.original_verdict !== 'drop'
      && value.original_verdict !== 'insufficient'
    )
    || (value.reason !== null && typeof value.reason !== 'string')
    || (value.rank !== undefined && value.rank !== null && nonNegativeInteger(value.rank) === null)
  ) return null
  const metrics = parseMetricRecord(value.metrics)
  const override = value.override == null ? null : parseOverride(value.override)
  if (value.override != null && override === null) return null
  return {
    code: value.code,
    name: value.name,
    verdict: value.verdict,
    original_verdict: value.original_verdict,
    reason: textOrNull(value.reason),
    rank: value.rank == null ? null : value.rank as number,
    metrics: metrics ?? {},
    metrics_unavailable_reason: metrics === null
      ? '指标值超出安全展示契约，已隐藏本行指标'
      : null,
    override,
  }
}

function parseRows(value: unknown): ParsedDiscoveryRow[] | null {
  if (!Array.isArray(value) || value.length > RESULT_MAX_ROWS) return null
  const rows = value.map(parseRow)
  return rows.some((row) => row === null) ? null : rows as ParsedDiscoveryRow[]
}

function parseDecision(value: unknown): ParsedDiscoveryDecision | null {
  if (
    !isRecord(value)
    || nonNegativeInteger(value.id) === null
    || !isTrack(value.track)
    || nonNegativeInteger(value.competition_stage_id) === null
    || nonNegativeInteger(value.competition_attempt) === null
    || typeof value.code !== 'string'
    || !value.code
    || (value.decision !== 'keep' && value.decision !== 'exclude')
    || (value.exclusion_reason !== null && typeof value.exclusion_reason !== 'string')
    || (value.keep_note !== null && typeof value.keep_note !== 'string')
  ) return null
  return value as unknown as ParsedDiscoveryDecision
}

function parseEvidence(value: unknown): ParsedDiscoveryEvidence | null {
  if (!isRecord(value)) return null
  const riskPrefix = `${String(value.track)}.risk.`
  const riskCategory = typeof value.rule_id === 'string' && value.rule_id.startsWith(riskPrefix)
    ? value.rule_id.slice(riskPrefix.length).split('.', 1)[0] ?? ''
    : ''
  if (
    nonNegativeInteger(value.id) === null
    || !isTrack(value.track)
    || nonNegativeInteger(value.competition_stage_id) === null
    || nonNegativeInteger(value.competition_attempt) === null
    || typeof value.code !== 'string'
    || !value.code
    || nonNegativeInteger(value.ruler_version_id) === null
    || value.rule_version !== EVIDENCE_RULE_VERSION
    || (value.kind !== 'fact' && value.kind !== 'risk')
    || typeof value.rule_id !== 'string'
    || !value.rule_id.startsWith(`${value.track}.${value.kind}.`)
    || !isRecord(value.input_fields)
    || !Object.keys(value.input_fields).length
    || typeof value.display_text !== 'string'
    || !value.display_text.trim()
    || typeof value.as_of !== 'string'
    || !isIsoDate(value.as_of)
    || (value.data_completeness !== 'complete' && value.data_completeness !== 'partial')
    || (value.kind === 'fact' && value.data_completeness !== 'complete')
    || (
      value.kind === 'risk'
      && (
        (riskCategory !== 'none' && !RISK_CATEGORIES.has(riskCategory))
        || (
          riskCategory === 'none'
          && (
            value.rule_id !== `${value.track}.risk.none`
            || value.display_text !== FALLBACK_RISK_TEXT
          )
        )
      )
    )
  ) return null
  return value as unknown as ParsedDiscoveryEvidence
}

function parseChange(value: unknown): ParsedDiscoveryChange | null {
  if (
    !isRecord(value)
    || typeof value.code !== 'string'
    || !value.code
    || typeof value.classification !== 'string'
    || !CHANGE_MEMBERSHIPS.includes(value.membership as DiscoveryMembership)
    || (
      value.cause !== null
      && !CHANGE_CAUSES.includes(value.cause as Exclude<DiscoveryChangeCause, null>)
    )
    || typeof value.reason_changed !== 'boolean'
    || !Array.isArray(value.classifications)
    || !['absent', 'pass', 'drop', 'insufficient'].includes(
      String(value.previous_state),
    )
    || !['absent', 'pass', 'drop', 'insufficient'].includes(
      String(value.current_state),
    )
    || !boundedChangeReason(value.previous_reason)
    || !boundedChangeReason(value.current_reason)
  ) return null
  const expected: DiscoveryChangeClassification[] = [
    value.membership as DiscoveryMembership,
    ...(value.reason_changed ? ['reason_changed' as const] : []),
    ...(value.cause === null ? [] : [value.cause as Exclude<DiscoveryChangeCause, null>]),
  ]
  if (
    value.classifications.length !== expected.length
    || value.classifications.some((item, index) => item !== expected[index])
    || value.classification !== (
      value.cause ?? (value.reason_changed ? 'reason_changed' : value.membership)
    )
  ) return null
  return {
    code: value.code,
    classification: value.classification as DiscoveryChangeClassification,
    classifications: expected,
    membership: value.membership as DiscoveryMembership,
    cause: value.cause as DiscoveryChangeCause,
    reason_changed: value.reason_changed,
    previous_state: value.previous_state as DiscoveryResultState,
    current_state: value.current_state as DiscoveryResultState,
    previous_reason: value.previous_reason as string | null,
    current_reason: value.current_reason as string | null,
  }
}

function boundedChangeReason(value: unknown): value is string | null {
  return value === null
    || (typeof value === 'string' && value.length <= CHANGE_REASON_MAX_LENGTH)
}

function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const timestamp = Date.parse(`${value}T00:00:00Z`)
  return Number.isFinite(timestamp)
    && new Date(timestamp).toISOString().slice(0, 10) === value
}

function parseSummary(value: unknown): ParsedStageResponse['summary'] | null {
  if (!isRecord(value) || !Array.isArray(value.rules) || !isRecord(value.ruler)
    || !isRecord(value.counts) || !isRecord(value.distributions)
    || !Array.isArray(value.threshold_markers)) return null
  const rules: { id: string; text: string }[] = []
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
  const distributions: Record<string, ParsedDistribution> = {}
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
  const markers: ParsedStageResponse['summary']['threshold_markers'] = []
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
    markers.push(marker as unknown as ParsedStageResponse['summary']['threshold_markers'][number])
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
    counts: parsedCounts as ParsedStageResponse['summary']['counts'],
    distributions,
    threshold_markers: markers,
  }
}

export function parseStageResponse(value: unknown): ParsedStageResponse | null {
  if (!isRecord(value) || !isRecord(value.current) || !isStageStatus(value.current.status)) {
    return null
  }
  const stage = value.current.stage
  const track = value.current.track
  if (
    (stage !== 'safety' && !isTrackStage(stage))
    || (stage === 'safety' && track !== undefined && track !== null)
    || (stage !== 'safety' && !isTrack(track))
  ) return null
  const requiresCardArrays = stage === 'competition' || stage === 'adjudication'
  if (
    requiresCardArrays
    && (
      !Array.isArray(value.decisions)
      || !Array.isArray(value.evidence)
      || !Array.isArray(value.changes)
    )
  ) return null
  if (
    nonNegativeInteger(value.current.id) === null
    || typeof value.current.input_fingerprint !== 'string'
    || !value.current.input_fingerprint
    || (
      value.current.ruler_version_id !== null
      && nonNegativeInteger(value.current.ruler_version_id) === null
    )
    || typeof value.current.mutation_version !== 'string'
    || !value.current.mutation_version
    || typeof value.current.stage_revision !== 'number'
    || !Number.isSafeInteger(value.current.stage_revision)
    || value.current.stage_revision < 0
  ) return null
  const passed = parseRows(value.pass)
  const dropped = parseRows(value.drop)
  const insufficient = parseRows(value.insufficient)
  const summary = parseSummary(value.summary)
  if (passed === null || dropped === null || insufficient === null || summary === null) return null
  const overrides = Array.isArray(value.overrides) ? value.overrides.map(parseOverride) : []
  const decisions = Array.isArray(value.decisions) ? value.decisions.map(parseDecision) : []
  const evidence = Array.isArray(value.evidence) ? value.evidence.map(parseEvidence) : []
  const changes = Array.isArray(value.changes) ? value.changes.map(parseChange) : []
  if (
    overrides.some((item) => item === null)
    || decisions.some((item) => item === null)
    || evidence.some((item) => item === null)
    || changes.some((item) => item === null)
  ) return null
  return {
    current: {
      id: value.current.id as number,
      track: stage === 'safety' ? null : track as DiscoveryTrack,
      stage,
      attempt: nonNegativeInteger(value.current.attempt) ?? 0,
      status: value.current.status,
      error: textOrNull(value.current.error),
      failure_kind: value.current.failure_kind === 'transient'
        || value.current.failure_kind === 'permanent'
        ? value.current.failure_kind
        : null,
      input_fingerprint: value.current.input_fingerprint,
      ruler_version_id: value.current.ruler_version_id as number | null,
      stage_revision: value.current.stage_revision as number,
      mutation_version: value.current.mutation_version,
    },
    pass: passed,
    drop: dropped,
    insufficient,
    overrides: overrides as ParsedDiscoveryOverride[],
    decisions: decisions as ParsedDiscoveryDecision[],
    evidence: evidence as ParsedDiscoveryEvidence[],
    changes: changes as ParsedDiscoveryChange[],
    summary,
  }
}

export function validateCandidateEvidence(
  payload: ParsedStageResponse,
): { valid: boolean; message: string } {
  if (payload.current.stage !== 'competition') {
    return { valid: false, message: '候选案例必须来自候选竞争阶段' }
  }
  for (const row of payload.pass) {
    const items = payload.evidence.filter(
      (item) => item.code === row.code
        && item.track === payload.current.track
        && item.competition_stage_id === payload.current.id
        && item.competition_attempt === payload.current.attempt
        && item.ruler_version_id === payload.current.ruler_version_id,
    )
    const facts = items.filter((item) => item.kind === 'fact')
    const risks = items.filter((item) => item.kind === 'risk')
    const identities = new Set(items.map((item) => `${item.kind}:${item.rule_id}`))
    if (
      row.reason === 'evidence_facts_insufficient'
      || facts.length < 3
      || facts.length > 5
      || risks.length < 1
      || identities.size !== items.length
    ) {
      return {
        valid: false,
        message: `${row.name} 缺少 3–5 条可靠事实或有效风险证据`,
      }
    }
  }
  return { valid: true, message: '' }
}
