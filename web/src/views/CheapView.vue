<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

interface CheapRow {
  code: string
  name: string
  price?: number | null
  pe?: number | null
  pb?: number | null
  mcap?: number | null
  industry?: string | null
  roe_mean?: number | null
  roe10?: number | null
  ocf_ni?: number | null
  score?: number | null
}

interface CheapLayer {
  key: string
  label: string
  note?: string
  count: number
  rows: CheapRow[]
}

const router = useRouter()
const sort = ref<'pe' | 'pb'>('pe')
const rows = ref<CheapRow[]>([])
const layers = ref<CheapLayer[]>([])
const activeLayer = ref('shortlist')
const universe = ref<number | null>(null)
const ready = ref<number | null>(null)
const pending = ref<number>(0)
const screened = ref<number | null>(null)
const passed = ref<number | null>(null)
const filling = ref(false)
const scopeLabel = ref('')
const errorText = ref('')
const loading = ref(false)
const evaluating = ref<string | null>(null)
const evalError = ref('')
let pollTimer: ReturnType<typeof setTimeout> | undefined

function formatNumber(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  return Number(value).toLocaleString('zh-CN', {
    maximumFractionDigits: 2,
    minimumFractionDigits: 0,
  })
}

function formatMcap(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return ''
  return `${(Number(value) / 1e8).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}亿`
}

const shown = computed<CheapLayer | null>(
  () => layers.value.find((layer) => layer.key === activeLayer.value) ?? null,
)
const shownRows = computed<CheapRow[]>(() => shown.value?.rows ?? rows.value)

function schedulePoll() {
  if (pollTimer != null) clearTimeout(pollTimer)
  if (pending.value > 0 || filling.value) {
    pollTimer = setTimeout(() => {
      void loadBoard(sort.value, true)
    }, 2000)
  }
}

async function loadBoard(next: 'pe' | 'pb' = sort.value, quiet = false) {
  if (!quiet) {
    loading.value = true
    errorText.value = ''
    evalError.value = ''
  }
  sort.value = next
  try {
    const response = await fetch(`/api/cheap?sort=${next}`)
    const data = await response.json()
    if (!response.ok) {
      if (!quiet) {
        errorText.value = data.detail || data.error || '无法读取低估值榜'
        rows.value = []
        layers.value = []
      }
      return
    }
    rows.value = Array.isArray(data.rows) ? data.rows : []
    layers.value = Array.isArray(data.layers) ? data.layers : []
    universe.value = typeof data.universe === 'number' ? data.universe : null
    ready.value = typeof data.ready === 'number' ? data.ready : data.pool
    pending.value = typeof data.pending === 'number' ? data.pending : 0
    screened.value = typeof data.screened === 'number' ? data.screened : null
    passed.value = typeof data.passed === 'number' ? data.passed : null
    filling.value = Boolean(data.filling)
    scopeLabel.value = data.scope_label || ''
  } catch {
    if (!quiet) {
      errorText.value = '无法读取低估值榜'
      rows.value = []
      layers.value = []
    }
  } finally {
    if (!quiet) loading.value = false
    schedulePoll()
  }
}

async function startEval(row: CheapRow) {
  if (evaluating.value) return
  evalError.value = ''
  evaluating.value = row.code
  try {
    const response = await fetch('/api/research', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: row.name, code: row.code }),
    })
    const data = await response.json()
    if (!response.ok) {
      evalError.value = data.detail || data.error || '评估失败'
      return
    }
    if (data.id == null) {
      evalError.value = '评估失败'
      return
    }
    await router.push(`/research/${data.id}`)
  } catch {
    evalError.value = '评估失败'
  } finally {
    evaluating.value = null
  }
}

onMounted(() => {
  void loadBoard('pe')
})

onBeforeUnmount(() => {
  if (pollTimer != null) clearTimeout(pollTimer)
})
</script>

<template>
  <section>
    <div class="toolbar">
      <h1>低估值榜</h1>
      <p class="muted">{{ scopeLabel || '沪深A股截面' }}</p>
    </div>
    <p class="muted">
      四层筛选，先粗后细：全市场截面 → 漏斗去劣（ROE / 现金流 / 毛利率等）→ 10 年平均 ROE ≥ 12% 且 5 年净现比 ≥ 1
      → 每行业最多 2 家、按性价比（10 年 ROE ÷ 估值）取前 30。缺指标一律不留。不跑 Checklist。
      每层都可以点开看名单，对照着找出是哪一层把某只票筛掉的。
    </p>
    <form class="inline-form" @submit.prevent>
      <button type="button" :disabled="loading || sort === 'pe'" @click="loadBoard('pe')">
        ROE ÷ PE
      </button>
      <button type="button" :disabled="loading || sort === 'pb'" @click="loadBoard('pb')">
        ROE ÷ PB
      </button>
    </form>
    <p v-if="loading">加载榜单…</p>
    <p v-if="errorText">{{ errorText }}</p>
    <p v-if="evalError">{{ evalError }}</p>
    <p v-if="!loading && !errorText && universe != null">
      正估值 {{ universe }} 家 → 已有财报 {{ ready ?? 0 }} 家 → 去劣 {{ screened ?? 0 }} 家
      → 过质量门槛 {{ passed ?? 0 }} 家 → 短名单 {{ rows.length }} 家。
      <template v-if="pending > 0 || filling">还在补 {{ pending }} 家财报。</template>
    </p>
    <form v-if="layers.length" class="inline-form" @submit.prevent>
      <button
        v-for="layer in layers"
        :key="layer.key"
        type="button"
        :disabled="activeLayer === layer.key"
        @click="activeLayer = layer.key"
      >
        {{ layer.label }} {{ layer.count }}
      </button>
    </form>
    <p v-if="shown?.note" class="muted">{{ shown.label }}：{{ shown.note }}</p>
    <table v-if="shownRows.length">
      <thead>
        <tr>
          <th>名称</th>
          <th>代码</th>
          <th>行业</th>
          <th>现价</th>
          <th>PE</th>
          <th>PB</th>
          <th>10年ROE</th>
          <th>净现比</th>
          <th>性价比</th>
          <th>市值</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in shownRows" :key="row.code">
          <td>{{ row.name }}</td>
          <td>{{ row.code }}</td>
          <td>{{ row.industry || '' }}</td>
          <td>{{ formatNumber(row.price) }}</td>
          <td>{{ formatNumber(row.pe) }}</td>
          <td>{{ formatNumber(row.pb) }}</td>
          <td>{{ formatNumber(row.roe10 ?? row.roe_mean) }}</td>
          <td>{{ formatNumber(row.ocf_ni) }}</td>
          <td>{{ formatNumber(row.score) }}</td>
          <td>{{ formatMcap(row.mcap) }}</td>
          <td>
            <button
              type="button"
              :disabled="evaluating != null"
              @click="startEval(row)"
            >
              评估
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
