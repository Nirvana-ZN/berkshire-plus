<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import SentimentPanel from '../components/SentimentPanel.vue'

interface BoardRow {
  name: string
  code: string
}

interface ScreenRow {
  code: string
  name: string
  verdict: string
  reason?: string | null
  role?: string | null
  finalist?: boolean
  mcap?: number | null
  industry?: string | null
  price?: number | null
  expected_price?: number | null
  gap_pct?: number | null
}

interface ResearchJobRow {
  id: number
  name: string
  code: string
  status: string
  error?: string | null
}

const ROLE_LABEL: Record<string, string> = {
  high_certainty: '高确定性',
  high_elasticity: '高弹性',
  mid_elasticity: '中弹性',
}

const JOB_STATUS: Record<string, string> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}

const router = useRouter()
const scopeType = ref('行业')
const scopeText = ref('')
const evaluate = ref(true)
const submitting = ref(false)
const singleQuery = ref('')
const singleBusy = ref(false)
const singleError = ref('')
const status = ref('')
const errorText = ref('')
const emptyHint = '尚未运行漏斗，此处不展示名单。'
const keep = ref<ScreenRow[]>([])
const drop = ref<ScreenRow[]>([])
const insufficient = ref<ScreenRow[]>([])
const finalists = ref<ScreenRow[]>([])
const jobs = ref<ResearchJobRow[]>([])
const activeId = ref<number | null>(null)
const boards = ref<BoardRow[]>([])
const boardError = ref('')
const rulerMatched = ref(true)
const rulerReason = ref('')
const rulerHint = ref('')
const rulerBusy = ref(false)
const sentiment = ref<{
  score: number | null
  zone?: string
  label: string
} | null>(null)
const gateOpen = ref(false)
const replayRows = ref<
  { name: string; was?: string | null; now?: string | null; cause: string }[]
>([])
const suggestOpen = ref(false)
const suggestIndex = ref(0)
let timer: number | undefined

const busy = computed(() => {
  if (status.value === 'queued' || status.value === 'running') {
    return true
  }
  return jobs.value.some((row) => row.status === 'queued' || row.status === 'running')
})

function formatGap(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return ''
  const rounded = Math.round(value * 10) / 10
  return `${rounded}%`
}

function roleLabel(role?: string | null): string {
  if (!role) return ''
  return ROLE_LABEL[role] || role
}

function applyScreen(data: {
  status?: string
  error?: string | null
  keep?: ScreenRow[]
  drop?: ScreenRow[]
  insufficient?: ScreenRow[]
  finalists?: ScreenRow[]
  jobs?: ResearchJobRow[]
  scope_type?: string
  scope_text?: string
}) {
  status.value = data.status || ''
  keep.value = data.keep ?? []
  drop.value = data.drop ?? []
  insufficient.value = data.insufficient ?? []
  finalists.value = data.finalists ?? []
  jobs.value = data.jobs ?? []
  const previousType = scopeType.value
  if (data.scope_type) {
    scopeType.value = data.scope_type
  }
  if (data.scope_text) {
    scopeText.value = data.scope_text
  }
  if (data.scope_type && data.scope_type !== previousType) {
    void loadBoards()
  }
  if (data.error) {
    errorText.value = data.error
  } else if (data.status === 'failed') {
    errorText.value = '漏斗失败'
  }
}

function stillBusy(data: { status?: string; jobs?: ResearchJobRow[] }) {
  if (data.status === 'queued' || data.status === 'running') {
    return true
  }
  return (data.jobs ?? []).some((row) => row.status === 'queued' || row.status === 'running')
}

function stopPoll() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

async function loadScreen(id: number) {
  try {
    const response = await fetch(`/api/screens/${id}`)
    const data = await response.json()
    if (activeId.value !== id) {
      return
    }
    if (!response.ok) {
      errorText.value = data.detail || data.error || '无法读取选股结果'
      stopPoll()
      return
    }
    applyScreen(data)
    if (!stillBusy(data)) {
      stopPoll()
    }
  } catch {
    if (activeId.value !== id) {
      return
    }
    errorText.value = '无法读取选股结果'
    stopPoll()
  }
}

async function loadLatest() {
  try {
    const response = await fetch('/api/screens/latest')
    if (response.status === 404) {
      return
    }
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '无法读取选股结果'
      return
    }
    const id = Number(data.id)
    if (!id) {
      return
    }
    activeId.value = id
    applyScreen(data)
    if (busy.value) {
      timer = window.setInterval(() => {
        void loadScreen(id)
      }, 800)
    }
  } catch {
    errorText.value = '无法读取选股结果'
  }
}

async function startSingle() {
  if (singleBusy.value || busy.value) {
    return
  }
  singleError.value = ''
  const query = singleQuery.value.trim()
  if (!query) {
    singleError.value = '名称或代码不能为空'
    return
  }
  singleBusy.value = true
  try {
    const response = await fetch('/api/research', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    })
    const data = await response.json()
    if (!response.ok) {
      singleError.value = data.detail || data.error || '评估失败'
      return
    }
    if (data.id == null) {
      singleError.value = '评估失败'
      return
    }
    await router.push(`/research/${data.id}`)
  } catch {
    singleError.value = '评估失败'
  } finally {
    singleBusy.value = false
  }
}

async function loadSentiment() {
  try {
    const response = await fetch('/api/sentiment')
    if (!response.ok) {
      sentiment.value = { score: null, label: '情绪不可用' }
      return
    }
    sentiment.value = await response.json()
  } catch {
    sentiment.value = { score: null, label: '情绪不可用' }
  }
}

async function loadRuler() {
  try {
    const response = await fetch('/api/ruler')
    const data = await response.json()
    if (!response.ok) {
      rulerMatched.value = false
      rulerHint.value = data.detail || data.error || '无法读取尺子版本'
      return
    }
    rulerMatched.value = Boolean(data.matched)
    gateOpen.value = Boolean(data.gate)
    replayRows.value = Array.isArray(data.rows) ? data.rows : []
    rulerHint.value = data.matched
      ? ''
      : '当前尺子与已生效版本不一致，写下改因后才能开始筛选'
  } catch {
    rulerMatched.value = false
    rulerHint.value = '无法读取尺子版本'
  }
}

async function activateRuler() {
  if (rulerBusy.value) {
    return
  }
  rulerBusy.value = true
  rulerHint.value = ''
  try {
    const response = await fetch('/api/ruler', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: rulerReason.value }),
    })
    const data = await response.json()
    if (!response.ok) {
      rulerMatched.value = false
      rulerHint.value = data.detail || data.error || '尺子未能生效'
      return
    }
    rulerMatched.value = Boolean(data.matched)
    gateOpen.value = Boolean(data.gate)
    replayRows.value = Array.isArray(data.rows) ? data.rows : []
    rulerHint.value = data.matched ? '' : '当前尺子尚未生效'
    if (data.matched) {
      rulerReason.value = ''
    }
  } catch {
    rulerMatched.value = false
    rulerHint.value = '尺子未能生效'
  } finally {
    rulerBusy.value = false
  }
}

async function submitScope() {
  if (submitting.value || busy.value || !rulerMatched.value) {
    return
  }
  errorText.value = ''
  submitting.value = true
  try {
    const response = await fetch('/api/screens/funnel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scope_type: scopeType.value,
        scope_text: scopeText.value,
        evaluate: evaluate.value,
      }),
    })
    const data = await response.json()
    if (!response.ok) {
      errorText.value = data.detail || data.error || '提交失败'
      if (data.code === 'RULER_MISMATCH') {
        rulerMatched.value = false
        rulerHint.value = data.detail || errorText.value
      }
      return
    }
    stopPoll()
    const id = Number(data.id)
    activeId.value = id
    status.value = data.status || 'queued'
    keep.value = []
    drop.value = []
    insufficient.value = []
    finalists.value = []
    jobs.value = []
    await loadScreen(id)
    if (busy.value) {
      timer = window.setInterval(() => {
        void loadScreen(id)
      }, 800)
    }
  } catch {
    errorText.value = '提交失败'
  } finally {
    submitting.value = false
  }
}

const filteredBoards = computed(() => {
  const query = scopeText.value.trim()
  const rows = !query
    ? boards.value
    : boards.value.filter((row) => row.name.includes(query))
  return rows.slice(0, 20)
})

function pickBoard(row: BoardRow) {
  scopeText.value = row.name
  suggestOpen.value = false
}

function openSuggest() {
  suggestOpen.value = true
  suggestIndex.value = 0
}

function closeSuggest() {
  window.setTimeout(() => {
    suggestOpen.value = false
  }, 120)
}

function moveSuggest(step: number) {
  const total = filteredBoards.value.length
  if (!total) {
    return
  }
  suggestOpen.value = true
  suggestIndex.value = (suggestIndex.value + step + total) % total
}

function pickHighlighted(event: Event) {
  const typed = scopeText.value.trim()
  if (filteredBoards.value.some((row) => row.name === typed)) {
    suggestOpen.value = false
    return
  }
  const row = filteredBoards.value[suggestIndex.value]
  if (suggestOpen.value && row) {
    event.preventDefault()
    pickBoard(row)
  }
}

async function loadBoards() {
  boardError.value = ''
  try {
    const response = await fetch(
      `/api/screens/boards?scope_type=${encodeURIComponent(scopeType.value)}`,
    )
    const data = await response.json()
    if (!response.ok) {
      boards.value = []
      boardError.value = data.detail || data.error || '板块列表不可用'
      return
    }
    boards.value = Array.isArray(data.boards) ? data.boards : []
  } catch {
    boards.value = []
    boardError.value = '板块列表不可用'
  }
}

function onScopeTypeChange() {
  scopeText.value = ''
  suggestOpen.value = false
  void loadBoards()
}

function onScopeInput() {
  openSuggest()
}

onMounted(() => {
  void loadBoards()
  void loadLatest()
  void loadRuler()
  void loadSentiment()
})

onBeforeUnmount(stopPoll)
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>选股</h1>
      <p v-if="status" class="muted">{{ scopeType }}　{{ scopeText }}</p>
    </div>
    <SentimentPanel :sentiment="sentiment" />
    <form class="inline-form" @submit.prevent="submitScope">
      <label>
        范围类型
        <select v-model="scopeType" @change="onScopeTypeChange">
          <option value="行业">行业</option>
          <option value="指数">指数</option>
          <option value="主题">主题</option>
        </select>
      </label>
      <label class="combo">
        范围
        <input
          v-model="scopeText"
          type="text"
          name="scope_text"
          autocomplete="off"
          placeholder="输入检索或点选"
          @focus="openSuggest"
          @input="onScopeInput"
          @blur="closeSuggest"
          @keydown.down.prevent="moveSuggest(1)"
          @keydown.up.prevent="moveSuggest(-1)"
          @keydown.enter="pickHighlighted"
          @keydown.escape="suggestOpen = false"
        />
        <ul v-if="suggestOpen && filteredBoards.length" class="suggest" role="listbox">
          <li
            v-for="(row, index) in filteredBoards"
            :key="row.code + row.name"
            :class="{ active: index === suggestIndex }"
            role="option"
            @mousedown.prevent="pickBoard(row)"
          >
            {{ row.name }}
          </li>
        </ul>
      </label>
      <label class="check-inline">
        <input v-model="evaluate" type="checkbox" />
        评估终选
      </label>
      <button type="submit" :disabled="submitting || busy || !rulerMatched">开始筛选</button>
    </form>
    <form v-if="!rulerMatched" class="inline-form" @submit.prevent="activateRuler">
      <label>
        改因
        <input v-model="rulerReason" type="text" name="ruler_reason" placeholder="为何接受当前尺子" />
      </label>
      <button type="submit" :disabled="rulerBusy">生效尺子</button>
    </form>
    <p v-if="rulerHint">{{ rulerHint }}</p>
    <p v-if="gateOpen">可信度闸已开：新评估不得写成通过，终选不得放宽。</p>
    <table v-if="replayRows.length">
      <thead>
        <tr>
          <th>名称</th>
          <th>原状态</th>
          <th>回放</th>
          <th>归因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in replayRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.was || '' }}</td>
          <td>{{ row.now || '' }}</td>
          <td>{{ { ruler: '尺子偏了', data: '数据变了', noise: '模型噪声', gap: '无法对质' }[row.cause] || row.cause }}</td>
        </tr>
      </tbody>
    </table>
    <form class="inline-form" @submit.prevent="startSingle">
      <label>
        个股
        <input
          v-model="singleQuery"
          type="text"
          name="single_query"
          placeholder="名称或代码，如 招商银行 / 600036.SH"
        />
      </label>
      <button type="submit" :disabled="singleBusy || busy">评估 Checklist</button>
    </form>
    <p v-if="singleError">{{ singleError }}</p>
    <p v-if="boardError" class="muted">{{ boardError }}</p>
    <p v-if="status">状态：{{ JOB_STATUS[status] || status }}</p>
    <p v-if="errorText">{{ errorText }}</p>
    <p v-if="!status && !errorText">{{ emptyHint }}</p>

    <template v-if="jobs.length && status !== 'done'">
      <h2>评估子任务</h2>
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>状态</th>
            <th>入口</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in jobs" :key="'j-' + row.id">
            <td>{{ row.name }}</td>
            <td>{{ JOB_STATUS[row.status] || row.status }}</td>
            <td>
              <router-link :to="'/research/' + row.id">查看评估</router-link>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-if="keep.length || drop.length || insufficient.length || finalists.length">
      <h2>终选</h2>
      <p v-if="finalists.length === 0">本轮无终选</p>
      <table v-else>
        <thead>
          <tr>
            <th>名称</th>
            <th>角色</th>
            <th>现价</th>
            <th>预期价格</th>
            <th>差距</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in finalists" :key="'f-' + row.code">
            <td>{{ row.name }}</td>
            <td>{{ roleLabel(row.role) }}</td>
            <td>{{ row.price ?? '' }}</td>
            <td>{{ row.expected_price ?? '' }}</td>
            <td>{{ formatGap(row.gap_pct) }}</td>
          </tr>
        </tbody>
      </table>

      <template v-if="jobs.length && status === 'done'">
        <h2>评估子任务</h2>
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>入口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in jobs" :key="'jd-' + row.id">
              <td>{{ row.name }}</td>
              <td>{{ JOB_STATUS[row.status] || row.status }}</td>
              <td>
                <router-link :to="'/research/' + row.id">查看评估</router-link>
              </td>
            </tr>
          </tbody>
        </table>
      </template>

      <h2>保留</h2>
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>代码</th>
            <th>角色</th>
            <th>终选</th>
            <th>现价</th>
            <th>预期价格</th>
            <th>差距</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in keep" :key="'k-' + row.code">
            <td>{{ row.name }}</td>
            <td>{{ row.code }}</td>
            <td>{{ roleLabel(row.role) }}</td>
            <td>{{ row.finalist ? '是' : '' }}</td>
            <td>{{ row.price ?? '' }}</td>
            <td>{{ row.expected_price ?? '' }}</td>
            <td>{{ formatGap(row.gap_pct) }}</td>
          </tr>
        </tbody>
      </table>

      <h2>淘汰</h2>
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in drop" :key="'d-' + row.code">
            <td>{{ row.name }}</td>
            <td>{{ row.reason }}</td>
          </tr>
        </tbody>
      </table>

      <h2>数据不足</h2>
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in insufficient" :key="'i-' + row.code">
            <td>{{ row.name }}</td>
            <td>{{ row.reason }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>
