<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

interface Gate {
  name: string
  status: string
  reason: string
}

const VERDICT_ZH: Record<string, string> = {
  pass: '通过',
  fail: '不通过',
  gray: '灰色地带',
}
const GATE_ZH: Record<string, string> = {
  pass: '通过',
  fail: '失败',
  gray: '灰色',
}
const BAND_ZH: Record<string, string> = {
  aggressive: '激进',
  steady: '稳健',
  conservative: '保守',
  watch: '观望',
}
const ROLE_ZH: Record<string, string> = {
  core: '核心',
  satellite: '卫星',
  skip: '不买',
}
const JOB_STATUS: Record<string, string> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}
const THESIS_ESTABLISHED = '已立为论文'
const THESIS_UPDATED = '已更新论文'
const VALUATION_ZH: Record<string, string> = {
  price: '现价',
  bps: '每股净资产',
  pb: '市净率',
  pe: '静态市盈率',
  pe_basis: '市盈率口径',
  eps_mid: '中周期每股收益',
  pe_mid: '中周期对照市盈率',
  pe_mid_basis: '中周期市盈率口径',
  npl_pct: '不良率',
  eps: '每股收益',
  period: '报告期',
}

const route = useRoute()
const errorText = ref('')
const status = ref('')
const name = ref('')
const verdict = ref('')
const markdown = ref('')
const gates = ref<Gate[]>([])
const mirror = ref<string[]>([])
const bandMode = ref('')
const memoRole = ref('')
const memoEssence = ref('')
const memoRisks = ref('')
const shouldEnter = ref('')
const funding = ref('')
const overlap = ref('')
const dataGrade = ref('')
const asOfDate = ref('')
const asOfPrice = ref<string | number | null>(null)
const valuation = ref<Record<string, string | number>>({})
const thesisMark = ref('')
const watching = ref(false)
const joined = ref(false)
const watchMessage = ref('')
let timer: number | undefined

const title = computed(() => name.value || '评估')
const canWatch = computed(
  () =>
    !joined.value &&
    status.value === 'done' &&
    (verdict.value === 'pass' || verdict.value === 'gray'),
)
const prefillTo = computed(() => {
  const query: Record<string, string> = {}
  if (name.value) query.name = name.value
  if (typeof asOfPrice.value === 'number' || typeof asOfPrice.value === 'string') {
    const price = String(asOfPrice.value).trim()
    if (price) query.price = price
  }
  return { path: '/positions', query }
})

function stopPoll() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

function applyResearch(data: Record<string, unknown>) {
  status.value = typeof data.status === 'string' ? data.status : ''
  name.value = typeof data.name === 'string' ? data.name : ''
  verdict.value = typeof data.verdict === 'string' ? data.verdict : ''
  markdown.value = typeof data.markdown === 'string' ? data.markdown : ''
  gates.value = Array.isArray(data.gates) ? (data.gates as Gate[]) : []
  mirror.value = Array.isArray(data.mirror_test) ? (data.mirror_test as string[]) : []
  const bands = data.price_bands
  bandMode.value =
    bands && typeof bands === 'object' && 'mode' in bands
      ? String((bands as { mode?: string }).mode || '')
      : ''
  const memo = data.memo
  if (memo && typeof memo === 'object') {
    const row = memo as Record<string, unknown>
    memoRole.value = typeof row.role === 'string' ? row.role : ''
    memoEssence.value = typeof row.essence === 'string' ? row.essence : ''
    memoRisks.value = typeof row.risks === 'string' ? row.risks : ''
  }
  const fit = data.portfolio_fit
  if (fit && typeof fit === 'object') {
    const row = fit as Record<string, unknown>
    shouldEnter.value = typeof row.should_enter === 'string' ? row.should_enter : ''
    funding.value = typeof row.funding === 'string' ? row.funding : ''
    overlap.value = typeof row.overlap === 'string' ? row.overlap : ''
  }
  dataGrade.value = typeof data.data_grade === 'string' ? data.data_grade : ''
  const asOf = data.as_of
  if (asOf && typeof asOf === 'object') {
    const row = asOf as Record<string, unknown>
    asOfDate.value = typeof row.date === 'string' ? row.date : ''
    asOfPrice.value = (row.price as string | number | null) ?? null
  }
  const snap = data.valuation
  valuation.value = {}
  if (snap && typeof snap === 'object') {
    for (const [key, value] of Object.entries(snap as Record<string, unknown>)) {
      if (value !== null && value !== undefined && value !== '') {
        valuation.value[key] = value as string | number
      }
    }
  }
  if (data.thesis_mark === THESIS_UPDATED) {
    thesisMark.value = THESIS_UPDATED
  } else if (data.thesis_mark === THESIS_ESTABLISHED) {
    thesisMark.value = THESIS_ESTABLISHED
  } else {
    thesisMark.value = ''
  }
  if (data.status === 'failed') {
    errorText.value = typeof data.error === 'string' ? data.error : '评估失败'
  }
}

async function loadResearch() {
  const id = route.params.id
  try {
    const response = await fetch(`/api/research/${id}`)
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '无法读取评估'
      status.value = 'failed'
      stopPoll()
      return
    }
    applyResearch(data)
    if (data.status === 'done' || data.status === 'failed') {
      stopPoll()
    }
  } catch {
    errorText.value = '无法读取评估'
    stopPoll()
  }
}

async function addToWatchlist() {
  if (!canWatch.value || watching.value) return
  watchMessage.value = ''
  watching.value = true
  try {
    const response = await fetch('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ research_id: Number(route.params.id) }),
    })
    const data = await response.json()
    if (!response.ok) {
      watchMessage.value = data.detail || data.error || '无法加入观察池'
      return
    }
    watchMessage.value = '已加入观察池'
    joined.value = true
  } catch {
    watchMessage.value = '无法加入观察池'
  } finally {
    watching.value = false
  }
}

onMounted(async () => {
  await loadResearch()
  if (status.value !== 'done' && status.value !== 'failed') {
    timer = window.setInterval(() => {
      void loadResearch()
    }, 800)
  }
})

watch(
  () => route.params.id,
  async () => {
    stopPoll()
    await loadResearch()
    if (status.value !== 'done' && status.value !== 'failed') {
      timer = window.setInterval(() => {
        void loadResearch()
      }, 800)
    }
  },
)

onBeforeUnmount(stopPoll)
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>{{ title }}</h1>
      <template v-if="status === 'done'">
        <button v-if="canWatch" type="button" :disabled="watching" @click="addToWatchlist">
          加入观察池
        </button>
        <RouterLink v-if="name" :to="prefillTo">预填记账</RouterLink>
      </template>
    </div>
    <p v-if="status">状态：{{ JOB_STATUS[status] || status }}</p>
    <p v-if="thesisMark" :class="{ 'thesis-pass': verdict === 'pass', 'thesis-fail': verdict === 'fail' }">{{ thesisMark }}</p>
    <p v-if="errorText">{{ errorText }}</p>
    <p v-if="watchMessage">{{ watchMessage }}</p>

    <template v-if="status === 'done'">
      <h2>结论条</h2>
      <table>
        <tbody>
          <tr>
            <th>结论</th>
            <td :class="{ 'thesis-pass': verdict === 'pass', 'thesis-fail': verdict === 'fail' }">{{ VERDICT_ZH[verdict] || verdict }}</td>
          </tr>
          <tr>
            <th>价格带</th>
            <td>{{ BAND_ZH[bandMode] || bandMode }}</td>
          </tr>
          <tr>
            <th>仓位角色</th>
            <td>{{ ROLE_ZH[memoRole] || memoRole }}</td>
          </tr>
          <tr>
            <th>信息丰富度</th>
            <td>{{ dataGrade }}</td>
          </tr>
          <tr>
            <th>研究日期</th>
            <td>{{ asOfDate }}</td>
          </tr>
          <tr>
            <th>所用股价</th>
            <td>{{ asOfPrice == null ? '无现价' : asOfPrice }}</td>
          </tr>
          <tr v-for="(value, key) in valuation" :key="'v-' + key">
            <th>{{ VALUATION_ZH[key] || key }}</th>
            <td>{{ value }}</td>
          </tr>
        </tbody>
      </table>

      <h2>对照当前仓</h2>
      <table>
        <tbody>
          <tr>
            <th>该不该进当前仓</th>
            <td>{{ shouldEnter }}</td>
          </tr>
          <tr>
            <th>用什么钱买</th>
            <td>{{ funding }}</td>
          </tr>
          <tr>
            <th>是否叠逻辑</th>
            <td>{{ overlap }}</td>
          </tr>
        </tbody>
      </table>

      <h2>备忘</h2>
      <table>
        <tbody>
          <tr>
            <th>生意本质</th>
            <td>{{ memoEssence }}</td>
          </tr>
          <tr>
            <th>主要风险</th>
            <td>{{ memoRisks }}</td>
          </tr>
        </tbody>
      </table>

      <details class="fold" open>
        <summary>六关</summary>
        <h2>六关</h2>
        <table>
          <thead>
            <tr>
              <th>关</th>
              <th>结果</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="gate in gates" :key="gate.name">
              <td>{{ gate.name }}</td>
              <td>{{ GATE_ZH[gate.status] || gate.status }}</td>
              <td>{{ gate.reason }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <details class="fold">
        <summary>镜子测试</summary>
        <h2>镜子测试</h2>
        <table>
          <tbody>
            <tr v-for="(line, index) in mirror" :key="'m-' + index">
              <td>{{ index + 1 }}</td>
              <td>{{ line }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <details class="fold">
        <summary>决策备忘录</summary>
        <h2>决策备忘录</h2>
        <article class="memo">{{ markdown }}</article>
      </details>
    </template>
  </section>
</template>

<style scoped>
.memo {
  white-space: pre-wrap;
  background: #fff;
  border: 1px solid #d8d2c4;
  padding: 1rem;
}
</style>
