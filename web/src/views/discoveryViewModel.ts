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
    attempt: number
    status: DiscoveryStageStatus
    error: string | null
    failure_kind: DiscoveryFailureKind
  }
  pass: ParsedDiscoveryRow[]
  drop: ParsedDiscoveryRow[]
  insufficient: ParsedDiscoveryRow[]
  overrides: ParsedDiscoveryOverride[]
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
const INVALID_METRIC = Symbol('invalid-metric')

export function stageCacheKey(
  runId: number,
  track: DiscoveryTrack | null,
  stage: DiscoveryStageName,
): string {
  return `${runId}:${track ?? 'common'}:${stage}`
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

export function armStageFetchTimeout(
  controller: AbortController,
  onTimeout: () => void = () => {},
  schedule: (callback: () => void, delay: number) => unknown =
    (callback, delay) => globalThis.setTimeout(callback, delay),
  cancel: (handle: unknown) => void =
    (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
): () => void {
  const handle = schedule(() => {
    onTimeout()
    controller.abort()
  }, STAGE_FETCH_TIMEOUT_MS)
  return () => cancel(handle)
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
  const passed = parseRows(value.pass)
  const dropped = parseRows(value.drop)
  const insufficient = parseRows(value.insufficient)
  const summary = parseSummary(value.summary)
  if (passed === null || dropped === null || insufficient === null || summary === null) return null
  const overrides = Array.isArray(value.overrides) ? value.overrides.map(parseOverride) : []
  if (overrides.some((item) => item === null)) return null
  return {
    current: {
      attempt: nonNegativeInteger(value.current.attempt) ?? 0,
      status: value.current.status,
      error: textOrNull(value.current.error),
      failure_kind: value.current.failure_kind === 'transient'
        || value.current.failure_kind === 'permanent'
        ? value.current.failure_kind
        : null,
    },
    pass: passed,
    drop: dropped,
    insufficient,
    overrides: overrides as ParsedDiscoveryOverride[],
    summary,
  }
}
