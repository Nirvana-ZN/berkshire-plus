import assert from 'node:assert/strict'
import test from 'node:test'
import { formatHorizon, formatPercent, parseTrackingResponse } from '../trackingViewModel.ts'
import { selectionAuditLines, selectionExclusionLabel } from '../discoveryViewModel.ts'

test('maturity, missing and unknown states never become zero returns', () => {
  assert.equal(formatHorizon({status: 'pending_maturity', net_return: null}), '尚未到期')
  assert.equal(formatHorizon({status: 'exit_pending', net_return: '0.1'}), '等待退出成交')
  assert.equal(formatHorizon({status: 'completed', net_return: null}), '数据不足')
  assert.equal(formatHorizon({status: 'completed', net_return: '0'}), '0.00%')
  assert.equal(formatHorizon({status: 'future_state', net_return: '0'}), '未知状态：future_state')
  assert.equal(formatPercent('NaN'), '数据不足')
})
test('legacy has no new conclusions and cap exclusion differs from threshold failure', () => {
  assert.deepEqual(selectionAuditLines(null), ['历史版本，无新增指标'])
  assert.notEqual(selectionExclusionLabel('industry_cap'), selectionExclusionLabel('gate_failed'))
  assert.match(selectionExclusionLabel('industry_cap'), /名额/)
  assert.match(selectionExclusionLabel('gate_failed'), /门槛/)
})
test('tracking rejects malformed collections and preserves empty state', () => {
  assert.equal(parseTrackingResponse({items: null}), null)
  assert.equal(parseTrackingResponse({items: [{id: 1}]}), null)
  assert.deepEqual(parseTrackingResponse({items: [], available_config_ids: ['paper-v1'], comparison: {groups: []}})?.items, [])
})

test('deteriorating risk stays explicit and a stored zero score is retained', () => {
  const lines = selectionAuditLines({operating_state: 'deteriorating', ranking_audit: {
    risk_state: 'deteriorating', rank_score: '0', verified_risk_types: ['interim_deterioration']}})
  assert.ok(lines.some((line) => line.includes('风险：恶化')))
  assert.ok(lines.some((line) => line.includes('：0')))
})
