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
