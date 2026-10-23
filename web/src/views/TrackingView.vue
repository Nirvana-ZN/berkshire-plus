<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { formatHorizon, formatPercent, KIND_LABELS, parseTrackingResponse, STATUS_LABELS,
  TRACK_LABELS, trackingStatus, VARIANT_LABELS, type TrackingCohort } from './trackingViewModel'

const route = useRoute()
const items = ref<TrackingCohort[]>([])
const configs = ref<string[]>(['paper-v1'])
const error = ref('')
const message = ref('')
const loading = ref(true)
const busy = ref(false)
const track = ref('')
const build = ref('')
const horizon = ref('')
const runId = ref('')
const registerTrack = ref('garp')
const variant = ref('full_selected')
const config = ref('paper-v1')
const activeCohort = ref(Number(route.query.cohort) || null)
const builds = computed(() => [...new Set(items.value.map((row) => row.algorithm_version))])
const visible = computed(() => items.value.filter((row) => (!track.value || row.track === track.value)
  && (!build.value || row.algorithm_version === build.value)
  && (!activeCohort.value || row.id === activeCohort.value)))
const ERROR_LABELS: Record<string, string> = {
  evidence_unavailable: '跟踪数据提供方暂不可用，保留已有观察。',
  historical_evidence_unavailable: '历史证据或发布时间不足，无法完成此请求。',
  tracking_busy: '已有任务运行，请稍后重试。', tracking_request_pending: '同一请求正在处理。',
  idempotency_conflict: '请求标识冲突，请重新操作。', run_not_found: '未找到该发现批次。',
  config_not_found: '未找到该冻结配置。', invalid_tracking_request: '请检查批次、轨道和配置。',
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await fetch('/api/tracking/cohorts')
    const parsed = parseTrackingResponse(await response.json())
    if (!response.ok || parsed === null) throw new Error('跟踪响应不可用')
    items.value = parsed.items
    configs.value = parsed.available_config_ids
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取失败'
  } finally { loading.value = false }
}
async function mutate(path: string, body: Record<string, unknown>) {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const response = await fetch(`/api/tracking/${path}`, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify(body)})
    const result = await response.json()
    if (!response.ok) {
      if (Array.isArray(result.updated_cohorts) && result.updated_cohorts.length) await load()
      throw new Error(ERROR_LABELS[result.code] || `请求未完成（${response.status}）`)
    }
    message.value = path === 'refresh' ? '观察已刷新。' : `已登记名单 #${result.cohort_id}。`
    await load()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '请求失败' }
  finally { busy.value = false }
}
function register() {
  const identity = Number(runId.value)
  if (!Number.isSafeInteger(identity) || identity <= 0) { error.value = '请输入有效批次编号。'; return }
  void mutate('cohorts', {run_id: identity, track: registerTrack.value,
    variant: variant.value, config_id: config.value})
}
function horizons(row: TrackingCohort) {
  return row.horizons.filter((item) => !horizon.value || item.horizon_months === Number(horizon.value))
}
onMounted(load)
</script>

<template>
  <section class="tracking">
    <div class="toolbar">
      <div><h1>独立跟踪</h1><p class="muted">纸面研究观察；先查看到期与数据覆盖状态。重叠样本仅作描述性比较。</p></div>
      <button type="button" :disabled="busy || loading" @click="mutate('refresh', {})">{{ busy ? '处理中…' : '刷新观察' }}</button>
    </div>
    <p v-if="error" class="alert-error" role="alert">{{ error }}</p>
    <p v-if="message" class="alert-ok" role="status">{{ message }}</p>
    <div class="panel filters">
      <label>轨道 <select v-model="track"><option value="">全部</option><option v-for="(label, key) in TRACK_LABELS" :key="key" :value="key">{{ label }}</option></select></label>
      <label>构建版本 <select v-model="build"><option value="">全部</option><option v-for="value in builds" :key="value">{{ value }}</option></select></label>
      <label>期限 <select v-model="horizon"><option value="">全部</option><option v-for="months in [3, 6, 12]" :key="months" :value="String(months)">{{ months }} 个月</option></select></label>
      <button v-if="activeCohort" type="button" @click="activeCohort = null">查看全部名单</button>
    </div>
    <p v-if="loading" role="status">正在读取跟踪…</p>
    <p v-else-if="!visible.length" class="panel">暂无跟踪名单。首次发布后自动登记；历史批次可按冻结配置登记为回顾观察。</p>
    <template v-for="(kindLabel, kind) in KIND_LABELS" :key="kind">
      <section v-if="visible.some((row) => row.kind === kind)" :aria-label="kindLabel">
        <h2>{{ kindLabel }}</h2>
        <article v-for="row in visible.filter((item) => item.kind === kind)" :key="row.id" class="panel cohort">
          <h3>#{{ row.id }} · {{ TRACK_LABELS[row.track] || row.track }} · {{ VARIANT_LABELS[row.variant_id] || row.variant_id }}</h3>
          <p><strong>{{ trackingStatus(row.status) }}</strong> · {{ kindLabel }} · {{ row.algorithm_version }} · 批次 #{{ row.run_id }} · 实验 #{{ row.experiment_id }}</p>
          <p>发布时间：{{ row.published_run_at || '不可用（无法确定入场时间）' }} · 原始成员：{{ row.original_member_count }}</p>
          <p v-if="!row.horizons.length">数据覆盖：尚无观察；{{ row.horizons_months.join(' / ') }} 个月期限尚无结果。</p>
          <div v-else class="table-scroll">
            <table>
              <thead><tr><th>期限 / 状态</th><th>入场覆盖</th><th>退出完成率</th><th>到期收益</th><th>截至观察日净收益</th><th>观察 / 退出日期</th></tr></thead>
              <tbody><tr v-for="observation in horizons(row)" :key="observation.horizon_months">
                <td>{{ observation.horizon_months }} 个月<br><strong>{{ trackingStatus(observation.status) }}</strong><p v-if="observation.data_gaps.length">缺口：{{ observation.data_gaps.join('；') }}</p></td>
                <td>{{ formatPercent(observation.entry_coverage) }}</td>
                <td>{{ formatPercent(observation.exit_completion_rate) }}</td>
                <td>{{ formatHorizon({status: observation.status, net_return: observation.horizon_return}) }}</td>
                <td>{{ formatPercent(observation.net_return) }}</td>
                <td>{{ observation.as_of }}<br>计划退出 {{ observation.expected_exit_date || '不可用' }}<br>实际退出 {{ observation.actual_exit_date || '尚未完成' }}</td>
              </tr></tbody>
            </table>
          </div>
          <details v-if="row.horizons.length"><summary>基准与相对收益（已核验序列版本）</summary>
            <div v-for="observation in horizons(row)" :key="observation.horizon_months">
              <p v-for="(benchmark, label) in observation.benchmarks" :key="label">{{ observation.horizon_months }} 个月 · {{ label === 'primary' ? '沪深300全收益' : '中证500全收益' }}：{{ benchmark.status === 'available' ? formatPercent(benchmark.return) : (benchmark.status === 'benchmark_revision_compatibility_unavailable' ? '基准版本兼容性不足' : '基准数据不足') }}；相对收益 {{ formatPercent(benchmark.relative_return) }}</p>
            </div>
          </details>
          <details><summary>冻结名单与复算标识</summary>
            <p v-if="!row.members.length">空名单保留原始记录。</p>
            <ul><li v-for="member in row.members" :key="member.code">{{ member.code }} · 初始权重 {{ formatPercent(member.weight) }} · {{ member.selection_reason }}</li></ul>
            <p>名单哈希：<code>{{ row.manifest_hash }}</code></p><p>协议哈希：<code>{{ row.protocol_hash }}</code></p>
          </details>
        </article>
      </section>
    </template>
    <details class="panel"><summary>登记历史发现名单</summary>
      <p>使用已冻结成员与服务端配置，登记结果属于回顾观察。名额排除不构成卖出信号。</p>
      <form class="filters" @submit.prevent="register">
        <label>发现批次 <input v-model="runId" inputmode="numeric" required></label>
        <label>轨道 <select v-model="registerTrack"><option v-for="(label, key) in TRACK_LABELS" :key="key" :value="key">{{ label }}</option></select></label>
        <label>名单组 <select v-model="variant"><option v-for="(label, key) in VARIANT_LABELS" :key="key" :value="key">{{ label }}</option></select></label>
        <label>冻结配置 <select v-model="config"><option v-for="value in configs" :key="value">{{ value }}</option></select></label>
        <button type="submit" :disabled="busy">登记</button>
      </form>
    </details>
    <p class="muted">数据与哈希仅说明记录完整性和绑定关系；独立来源真实性、审查独立性与结果有效性仍需核验。</p>
  </section>
</template>

<style scoped>
.filters { display: flex; flex-wrap: wrap; align-items: end; gap: 1rem; }
label { display: grid; gap: .35rem; min-width: 8rem; }
input, select { max-width: 100%; min-height: 2.4rem; }
.table-scroll { max-width: 100%; overflow-x: auto; }
table { min-width: 680px; }
.cohort { min-width: 0; }
p, code, li { overflow-wrap: anywhere; }
.toolbar { flex-wrap: wrap; }
</style>
