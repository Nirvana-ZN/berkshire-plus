import assert from 'node:assert/strict'
import test from 'node:test'

import {
  discoveryStatusText,
  jobStatusText,
  WORKBENCH_FETCH_TIMEOUT_MS,
  WORKBENCH_PATHS,
  WORKBENCH_REFRESH_MS,
} from '../homeViewModel.ts'

test('known home job statuses are selected by job type in Chinese', () => {
  assert.deepEqual(
    ['queued', 'running', 'waiting_confirmation', 'completed', 'partially_completed', 'failed', 'abandoned']
      .map((status) => discoveryStatusText(status)),
    ['排队中', '进行中', '等待确认', '已完成', '部分完成', '失败', '已放弃'],
  )
  assert.equal(jobStatusText('discovery', 'completed'), '已完成')
  assert.equal(jobStatusText('research', 'done'), '完成')
  assert.equal(jobStatusText('review', 'queued'), '排队')
  assert.equal(jobStatusText('discovery', 'future_status'), 'future_status')
})

test('workbench refresh generates one bounded seven-request batch', () => {
  assert.deepEqual(WORKBENCH_PATHS, [
    '/api/health',
    '/api/jobs',
    '/api/watchlist',
    '/api/reviews/latest',
    '/api/sentiment',
    '/api/aftermath',
    '/api/discoveries/current',
  ])
  assert.equal(new Set(WORKBENCH_PATHS).size, 7)
  assert.equal(WORKBENCH_REFRESH_MS, 3_000)
  assert.ok(WORKBENCH_FETCH_TIMEOUT_MS > 0)
  assert.ok(WORKBENCH_FETCH_TIMEOUT_MS < WORKBENCH_REFRESH_MS)
})
