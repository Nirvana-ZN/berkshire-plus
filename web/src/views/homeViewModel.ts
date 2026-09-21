const JOB_STATUS: Readonly<Record<string, string>> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}

const DISCOVERY_STATUS: Readonly<Record<string, string>> = {
  queued: '排队中',
  running: '进行中',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  partially_completed: '部分完成',
  failed: '失败',
  abandoned: '已放弃',
}

export const WORKBENCH_REFRESH_MS = 3_000
export const WORKBENCH_FETCH_TIMEOUT_MS = 2_500
export const WORKBENCH_PATHS = [
  '/api/health',
  '/api/jobs',
  '/api/watchlist',
  '/api/reviews/latest',
  '/api/sentiment',
  '/api/aftermath',
  '/api/discoveries/current',
] as const

export function discoveryStatusText(status: string): string {
  return DISCOVERY_STATUS[status] || status
}

export function jobStatusText(type: string, status: string): string {
  return type === 'discovery'
    ? discoveryStatusText(status)
    : JOB_STATUS[status] || status
}
