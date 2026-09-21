import assert from 'node:assert/strict'
import test from 'node:test'

import {
  armFetchTimeout,
  armStageFetchTimeout,
  canAbandonRun,
  candidateSurfacedReason,
  collectMetricKeys,
  CURRENT_FETCH_TIMEOUT_MS,
  latestStagesNeedPolling,
  keeperRange,
  matchesStagePointer,
  parseMetricRecord,
  parseStageResponse,
  renderMetricValue,
  reviewedStageActions,
  RESULT_METRIC_KEYS_MAX,
  RESULT_OBSERVATIONS_MAX,
  stageActions,
  stageCacheKey,
  sameStageReview,
  STAGE_FETCH_TIMEOUT_MS,
  shouldPollStage,
  validateCandidateEvidence,
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
  assert.deepEqual(
    reviewedStageActions(false, 'track_gate', 'waiting_confirmation', null),
    { confirm: false, override: false, retry: false },
  )
})

test('keeper range distinguishes insufficient, valid, and over-limit', () => {
  assert.equal(keeperRange(2), 'insufficient')
  assert.equal(keeperRange(3), 'valid')
  assert.equal(keeperRange(5), 'valid')
  assert.equal(keeperRange(6), 'over-limit')
})

test('review identity mismatch fails closed before mutation', () => {
  const reviewed = {
    runId: 7,
    track: 'garp' as const,
    stage: 'competition' as const,
    stageId: 42,
    attempt: 2,
    status: 'waiting_confirmation' as const,
    inputFingerprint: 'inputs-a',
    rulerVersionId: 5,
    stageRevision: 1_700_000_000_000_000,
    mutationVersion: 'mutation-a',
    fingerprint: 'payload-a',
  }
  assert.equal(sameStageReview(reviewed, { ...reviewed }), true)
  assert.equal(sameStageReview(reviewed, { ...reviewed, runId: 8 }), false)
  assert.equal(sameStageReview(reviewed, { ...reviewed, stage: 'adjudication' }), false)
  assert.equal(sameStageReview(reviewed, { ...reviewed, attempt: 3 }), false)
  assert.equal(
    sameStageReview(reviewed, { ...reviewed, mutationVersion: 'mutation-b' }),
    false,
  )
  assert.equal(sameStageReview(reviewed, { ...reviewed, fingerprint: 'payload-b' }), false)
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
      id: 42,
      stage: 'safety',
      attempt: 1,
      status: 'waiting_confirmation',
      error: null,
      failure_kind: null,
      input_fingerprint: 'inputs-a',
      ruler_version_id: 5,
      stage_revision: 1_700_000_000_000_000,
      mutation_version: 'mutation-a',
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

function competitionPayload() {
  const payload = stagePayload({})
  payload.current = {
    ...payload.current,
    stage: 'competition',
    track: 'garp',
    ruler_version_id: 5,
  }
  return {
    ...payload,
    decisions: [],
    evidence: [
      ...Array.from({ length: 3 }, (_, index) => ({
        id: index + 1,
        track: 'garp',
        competition_stage_id: 42,
        competition_attempt: 1,
        code: '000001',
        ruler_version_id: 5,
        rule_version: 'deterministic-evidence-v2',
        kind: 'fact',
        rule_id: `garp.fact.fixture_${index}`,
        input_fields: { source: 'financial' },
        display_text: `fact ${index}`,
        as_of: '2026-09-17',
        data_completeness: 'complete',
      })),
      {
        id: 4,
        track: 'garp',
        competition_stage_id: 42,
        competition_attempt: 1,
        code: '000001',
        ruler_version_id: 5,
        rule_version: 'deterministic-evidence-v2',
        kind: 'risk',
        rule_id: 'garp.risk.none',
        input_fields: { source_as_of: { financial: '2026-09-17' } },
        display_text: '尚无结构化反方证据',
        as_of: '2026-09-17',
        data_completeness: 'complete',
      },
    ],
    changes: [] as unknown[],
  }
}

test('competition schema requires exact card arrays and reliable evidence', () => {
  const payload = competitionPayload()
  payload.changes = [{
    code: '000001',
    name: '示例公司',
    classification: 'both_changed',
    classifications: ['continuous', 'reason_changed', 'both_changed'],
    membership: 'continuous',
    cause: 'both_changed',
    reason_changed: true,
    previous_state: 'drop',
    current_state: 'pass',
    previous_reason: '估值偏高',
    current_reason: '估值回落',
  }]
  const valid = parseStageResponse(payload)
  assert.ok(valid)
  assert.equal(validateCandidateEvidence(valid).valid, true)
  assert.deepEqual(valid.changes[0]!.classifications, [
    'continuous',
    'reason_changed',
    'both_changed',
  ])
  assert.equal(valid.changes[0]!.previous_state, 'drop')
  assert.equal(valid.changes[0]!.current_reason, '估值回落')
  assert.equal(valid.changes[0]!.name, '示例公司')

  for (const key of ['decisions', 'evidence', 'changes'] as const) {
    const malformed = competitionPayload()
    delete (malformed as Record<string, unknown>)[key]
    assert.equal(parseStageResponse(malformed), null)
  }

  const tooFewFacts = competitionPayload()
  tooFewFacts.evidence.splice(0, 1)
  const parsed = parseStageResponse(tooFewFacts)
  assert.ok(parsed)
  assert.equal(validateCandidateEvidence(parsed).valid, false)

  const unknownChange = competitionPayload()
  unknownChange.changes = [{
    code: '000001',
    classification: 'entered',
    classifications: ['entered'],
    membership: 'entered',
    cause: null,
    reason_changed: false,
    previous_state: 'absent',
    current_state: 'pass',
    previous_reason: null,
    current_reason: null,
  }]
  assert.equal(parseStageResponse(unknownChange), null)

  const oversizedReason = competitionPayload()
  oversizedReason.changes = [{
    code: '000001',
    classification: 'continuous',
    classifications: ['continuous'],
    membership: 'continuous',
    cause: null,
    reason_changed: false,
    previous_state: 'pass',
    current_state: 'pass',
    previous_reason: 'x'.repeat(2_001),
    current_reason: null,
  }]
  assert.equal(parseStageResponse(oversizedReason), null)
})

test('case surfaced reason is specific and honors forced-pass provenance', () => {
  const parsed = parseStageResponse(competitionPayload())
  assert.ok(parsed)
  const row = parsed.pass[0]!
  assert.match(candidateSurfacedReason(row, parsed.evidence), /fact 0/)
  assert.doesNotMatch(candidateSurfacedReason(row, parsed.evidence), /固定尺子/)

  const forced = {
    ...row,
    original_verdict: 'insufficient' as const,
    reason: 'evidence_facts_insufficient',
    override: {
      id: 9,
      track: 'garp' as const,
      stage: 'competition' as const,
      attempt: 1,
      code: row.code,
      action: 'forced_pass' as const,
      reason: '人工确认补充材料',
    },
  }
  assert.equal(
    candidateSurfacedReason(forced, parsed.evidence),
    '人工覆盖改为入围：人工确认补充材料（原判定：数据不足）',
  )
})

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

test('current fetch timeout aborts on the bounded recovery deadline', () => {
  const controller = new AbortController()
  let scheduled: (() => void) | undefined
  let delay = 0
  const cancel = armFetchTimeout(
    controller,
    CURRENT_FETCH_TIMEOUT_MS,
    () => {},
    (callback, milliseconds) => {
      scheduled = callback
      delay = milliseconds
      return 1
    },
    () => {},
  )
  assert.equal(delay, CURRENT_FETCH_TIMEOUT_MS)
  scheduled!()
  assert.equal(controller.signal.aborted, true)
  cancel()
})
