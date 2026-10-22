export interface TrackingHorizon {
  horizon_months: number
  status: string
  net_return: string | null
  horizon_return: string | null
  entry_coverage: string | null
  exit_completion_rate: string | null
  as_of: string
  target_date: string | null
  expected_exit_date: string | null
  actual_exit_date: string | null
  data_gaps: string[]
  benchmarks: Record<string, {status: string; return: string | null; relative_return: string | null}>
}
export interface TrackingCohort {
  id: number
  experiment_id: number
  run_id: number
  track: string
  variant_id: string
  kind: string
  algorithm_version: string
  status: string
  published_run_at: string | null
  publication_time_status: string
  original_member_count: number
  manifest_hash: string
  protocol_hash: string
  horizons: TrackingHorizon[]
  horizons_months: number[]
  members: {code: string; weight: string; selection_reason: string}[]
}
export const TRACK_LABELS: Record<string, string> = {
  quality_discount: '优质折价', deep_value: '深度价值', garp: '合理价成长',
}
export const VARIANT_LABELS: Record<string, string> = {
  full_selected: '正式入选组', no_operating_selected: '去当期经营层组', all_passed: '全通过名单',
}
export const KIND_LABELS: Record<string, string> = {
  prospective: '前瞻跟踪', retrospective_observation: '回顾观察',
}
export const STATUS_LABELS: Record<string, string> = {
  pending: '尚无观察数据', observed: '已有观察数据', completed: '到期且退出完成',
  pending_maturity: '尚未到期', exit_pending: '等待退出成交',
  entry_time_unavailable: '发布时间不可用', insufficient_data: '数据不足',
  data_incomplete: '数据不足', entry_pending: '等待入场成交',
  pending_entry: '等待入场成交', unresolved_delisting: '退市回收尚未解决', empty_cohort: '空名单',
}
export function formatPercent(value: string | null): string {
  if (value === null || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value)
      || !Number.isFinite(Number(value) * 100)) return '数据不足'
  return `${(Number(value) * 100).toFixed(2)}%`
}
export function formatHorizon(row: {status: string; net_return: string | null}): string {
  if (row.status === 'pending_maturity') return '尚未到期'
  if (row.status === 'exit_pending') return '等待退出成交'
  if (!Object.hasOwn(STATUS_LABELS, row.status)) return `未知状态：${row.status}`
  if (row.status !== 'completed') return STATUS_LABELS[row.status] || '数据不足'
  return formatPercent(row.net_return)
}
export function trackingStatus(status: string): string {
  return Object.hasOwn(STATUS_LABELS, status) ? STATUS_LABELS[status]! : `未知状态：${status}`
}
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
export function parseTrackingResponse(value: unknown): {
  items: TrackingCohort[]; available_config_ids: string[]
} | null {
  if (!record(value) || !Array.isArray(value.items) || !Array.isArray(value.available_config_ids)
      || value.available_config_ids.some((item) => typeof item !== 'string')) return null
  for (const item of value.items) {
    if (!record(item) || !Number.isSafeInteger(item.id) || !Number.isSafeInteger(item.experiment_id)
        || typeof item.track !== 'string' || !Object.hasOwn(TRACK_LABELS, item.track)
        || typeof item.kind !== 'string' || !Object.hasOwn(KIND_LABELS, item.kind)
        || typeof item.variant_id !== 'string' || !Object.hasOwn(VARIANT_LABELS, item.variant_id)
        || typeof item.algorithm_version !== 'string' || typeof item.status !== 'string'
        || !Array.isArray(item.members) || !Array.isArray(item.horizons)
        || !Array.isArray(item.horizons_months) || item.horizons_months.some((months) => ![3, 6, 12].includes(months))
        || !Number.isSafeInteger(item.original_member_count) || typeof item.manifest_hash !== 'string'
        || typeof item.protocol_hash !== 'string' || typeof item.publication_time_status !== 'string'
        || (item.published_run_at !== null && typeof item.published_run_at !== 'string')) return null
    if (item.members.some((member) => !record(member) || typeof member.code !== 'string'
      || typeof member.weight !== 'string' || typeof member.selection_reason !== 'string')) return null
    for (const row of item.horizons) {
      if (!record(row) || ![3, 6, 12].includes(row.horizon_months as number)
          || typeof row.status !== 'string' || (row.net_return !== null && typeof row.net_return !== 'string')
          || !Array.isArray(row.data_gaps) || row.data_gaps.some((gap) => typeof gap !== 'string')
          || typeof row.as_of !== 'string' || !record(row.benchmarks)) return null
      for (const field of ['horizon_return', 'entry_coverage', 'exit_completion_rate', 'target_date',
        'expected_exit_date', 'actual_exit_date']) {
        if (row[field] !== null && typeof row[field] !== 'string') return null
      }
      for (const benchmark of Object.values(row.benchmarks)) {
        if (!record(benchmark) || typeof benchmark.status !== 'string'
          || (benchmark.return !== null && typeof benchmark.return !== 'string')
          || (benchmark.relative_return !== null && typeof benchmark.relative_return !== 'string')) return null
      }
    }
  }
  return {items: value.items as TrackingCohort[], available_config_ids: value.available_config_ids as string[]}
}
