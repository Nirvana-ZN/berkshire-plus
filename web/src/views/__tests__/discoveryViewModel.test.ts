import assert from 'node:assert/strict'
import test from 'node:test'

import {
  canAbandonRun,
  matchesStagePointer,
  stageActions,
  stageCacheKey,
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
})
