import assert from 'node:assert/strict'
import test from 'node:test'

import {
  armStageFetchTimeout,
  canAbandonRun,
  collectMetricKeys,
  latestStagesNeedPolling,
  matchesStagePointer,
  parseMetricRecord,
  parseStageResponse,
  renderMetricValue,
  RESULT_METRIC_KEYS_MAX,
  RESULT_OBSERVATIONS_MAX,
  stageActions,
  stageCacheKey,
  STAGE_FETCH_TIMEOUT_MS,
  shouldPollStage,
} from '../discoveryViewModel.ts'

test('stage cache keys include run, track, and stage', () => {
  assert.notEqual(
    stageCacheKey(1, 'garp', 'track_gate'),
    stageCacheKey(2, 'garp', 'track_gate'),
  )
  assert.notEqual(
    stageCacheKey(1, 'garp', 'track_gate'),
    stageCacheKey(1, 'garp', 'competition'),
  )
  assert.equal(matchesStagePointer(2, 'queued', 1, 'failed'), false)
  assert.equal(matchesStagePointer(2, 'queued', 2, 'queued'), true)
})

test('only actionable stage states expose controls', () => {
  assert.deepEqual(stageActions('track_gate', 'waiting_confirmation', null), {
    confirm: true,
    override: true,
    retry: false,
  })
  assert.deepEqual(stageActions('adjudication', 'waiting_confirmation', null), {
    confirm: false,
    override: false,
    retry: false,
  })
  assert.equal(stageActions('competition', 'failed', 'transient').retry, true)
  assert.equal(stageActions('competition', 'failed', 'permanent').retry, false)
  assert.equal(stageActions('adjudication', 'failed', 'transient').retry, false)
})

test('polling and abandon follow backend mutability', () => {
  assert.equal(shouldPollStage(true, 'queued'), true)
  assert.equal(shouldPollStage(true, 'waiting_confirmation'), false)
  assert.equal(shouldPollStage(false, 'queued'), false)
  assert.equal(canAbandonRun(true, 'partially_completed'), true)
  assert.equal(canAbandonRun(false, 'running'), false)
  assert.equal(canAbandonRun(true, 'abandoned'), false)
  assert.equal(latestStagesNeedPolling([
    { created: true, status: 'waiting_confirmation' },
    { created: true, status: 'running' },
  ]), true)
  assert.equal(latestStagesNeedPolling([
    { created: true, status: 'waiting_confirmation' },
    { created: false, status: 'queued' },
  ]), false)
})

test('metric parser accepts backend boundaries and rejects non-contract values', () => {
  const parsed = parseMetricRecord({
    risk_flags: ['margin_deterioration', ['bank_asset_quality']],
    score: 1.25,
    active: true,
    note: null,
  })
  assert.ok(parsed)
  assert.equal(
    renderMetricValue(parsed.risk_flags!),
    '[margin_deterioration、[bank_asset_quality]]',
  )
  assert.equal(renderMetricValue(parsed.active!), '是')
  assert.ok(parseMetricRecord({
    observations: Array.from({ length: RESULT_OBSERVATIONS_MAX }, (_, index) => index),
  }))
  assert.ok(parseMetricRecord(Object.fromEntries(
    Array.from(
      { length: RESULT_METRIC_KEYS_MAX },
      (_, index) => [`metric_${index}`, Array.from({ length: 64 }, () => 0)],
    ),
  )))
  assert.equal(parseMetricRecord({ unsafe: { nested: true } }), null)
  assert.equal(parseMetricRecord({ nonfinite: Number.POSITIVE_INFINITY }), null)
  assert.equal(parseMetricRecord({
    tooWide: Array.from({ length: RESULT_OBSERVATIONS_MAX + 1 }, () => 'x'),
  }), null)
  assert.equal(
    parseMetricRecord({ tooMuchText: Array.from({ length: 17 }, () => 'x'.repeat(4096)) }),
    null,
  )
})

function stagePayload(metrics: unknown) {
  const row = {
    code: '000001',
    name: '示例公司',
    verdict: 'pass',
    original_verdict: 'pass',
    reason: null,
    rank: null,
    metrics,
    override: null,
  }
  return {
    current: {
      attempt: 1,
      status: 'waiting_confirmation',
      error: null,
      failure_kind: null,
    },
    pass: [row],
    drop: [],
    insufficient: [],
    overrides: [],
    summary: {
      schema_version: 'discovery-stage-summary.v1',
      rules: [],
      ruler: {
        version_id: 1,
        reason: 'weekly',
        scope: 'common',
        algorithm_version: 'v1',
        status: 'available',
      },
      counts: { input: 1, pass: 1, drop: 0, insufficient: 0, overrides: 0 },
      distributions: {},
      threshold_markers: [],
    },
  }
}

test('one invalid metric row stays visible with an explicit unavailable reason', () => {
  const parsed = parseStageResponse(stagePayload({
    observations: Array.from({ length: RESULT_OBSERVATIONS_MAX + 1 }, () => 0),
  }))
  assert.ok(parsed)
  assert.deepEqual(parsed.pass[0]!.metrics, {})
  assert.match(parsed.pass[0]!.metrics_unavailable_reason!, /已隐藏本行指标/)
})

test('metric key aggregation enumerates each row once at thousands-row scale', () => {
  let enumerations = 0
  const rows = Array.from({ length: 5_000 }, (_, index) => ({
    metrics: new Proxy(
      { shared: 1, [`metric_${index % 10}`]: index },
      {
        ownKeys(target) {
          enumerations += 1
          return Reflect.ownKeys(target)
        },
      },
    ),
  }))
  assert.equal(collectMetricKeys(rows).length, 11)
  assert.equal(enumerations, rows.length)
})

test('stage fetch timeout is bounded, abortable, and cancellable', () => {
  const controller = new AbortController()
  let scheduled: (() => void) | undefined
  let delay = 0
  let cancelled: unknown
  let timedOut = false
  const cancel = armStageFetchTimeout(
    controller,
    () => {
      timedOut = true
    },
    (callback, milliseconds) => {
      scheduled = callback
      delay = milliseconds
      return 'timer'
    },
    (handle) => {
      cancelled = handle
    },
  )
  assert.equal(delay, STAGE_FETCH_TIMEOUT_MS)
  scheduled!()
  assert.equal(timedOut, true)
  assert.equal(controller.signal.aborted, true)
  cancel()
  assert.equal(cancelled, 'timer')
})
