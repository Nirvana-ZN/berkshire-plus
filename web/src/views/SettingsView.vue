<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

const yamlDir = ref<string | null>(null)
const migrated = ref(false)
const error = ref('')
const detail = ref('')
const loadError = ref('')
const formError = ref('')
const message = ref('')
const hasApiKey = ref(false)
const hasSearchKey = ref(false)
const webSearch = ref(false)
const model = ref('')
const baseUrl = ref('')
const apiKey = ref('')
const searchKey = ref('')
const saving = ref(false)
const ready = ref(false)

const statusText = computed(() => (migrated.value ? '已迁入' : '未迁入'))

function applySettings(data: Record<string, unknown>) {
  yamlDir.value = (data.yaml_dir as string | null) ?? null
  migrated.value = Boolean(data.migrated)
  hasApiKey.value = Boolean(data.has_api_key)
  hasSearchKey.value = Boolean(data.has_search_key)
  webSearch.value = Boolean(data.web_search)
  model.value = typeof data.llm_model === 'string' ? data.llm_model : ''
  baseUrl.value = typeof data.llm_base_url === 'string' ? data.llm_base_url : ''
  error.value = typeof data.error === 'string' ? data.error : ''
  detail.value = typeof data.detail === 'string' ? data.detail : ''
}

async function loadSettings() {
  const response = await fetch('/api/settings')
  if (!response.ok) {
    throw new Error('load')
  }
  applySettings(await response.json())
}

async function saveSettings() {
  if (!ready.value) {
    return
  }
  formError.value = ''
  message.value = ''
  saving.value = true
  try {
    const body: Record<string, unknown> = {
      model: model.value,
      web_search: webSearch.value,
    }
    const typed = apiKey.value.trim()
    if (typed) {
      body.api_key = typed
    }
    const typedSearch = searchKey.value.trim()
    if (typedSearch) {
      body.search_api_key = typedSearch
    }
    const typedUrl = baseUrl.value.trim()
    if (typedUrl) {
      body.base_url = typedUrl
    }
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await response.json()
    if (!response.ok) {
      formError.value = data.detail || data.error || '保存失败'
      return
    }
    applySettings(data)
    message.value = typeof data.message === 'string' ? data.message : '已保存'
    apiKey.value = ''
    searchKey.value = ''
  } catch {
    formError.value = '保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    await loadSettings()
    ready.value = true
  } catch {
    loadError.value = '无法加载设置'
  }
})
</script>

<template>
  <section class="page-settings">
    <div class="toolbar">
      <h1>设置</h1>
    </div>
    <p v-if="loadError" class="alert-error">{{ loadError }}</p>
    <template v-else-if="ready">
      <form class="settings-grid" @submit.prevent="saveSettings">
        <div class="panel settings-card">
          <h2>大模型接口配置</h2>
          <div class="stack-form">
            <label>
              <span class="label-row">
                <span>API 密钥</span>
                <span v-if="hasApiKey" class="badge badge-pass">已保存</span>
              </span>
              <input v-model="apiKey" type="password" autocomplete="off" placeholder="留空则保持已存密钥不变" />
            </label>
            <label>
              模型
              <input v-model="model" placeholder="可选，留空用服务端默认" />
            </label>
            <label>
              接口地址
              <input
                v-model="baseUrl"
                placeholder="https://open.bigmodel.cn/api/coding/paas/v4"
              />
            </label>
            <p class="muted field-note">智谱 Coding Plan 用 /api/coding/paas/v4；按量计费用 /api/paas/v4</p>
          </div>
        </div>

        <div class="panel settings-card">
          <h2>网页搜索与数据迁入</h2>
          <div class="stack-form">
            <label>
              <span class="label-row">
                <span>Tavily 密钥</span>
                <span v-if="hasSearchKey" class="badge badge-pass">搜索密钥已保存</span>
              </span>
              <input v-model="searchKey" type="password" autocomplete="off" placeholder="留空则保持已存密钥不变" />
            </label>
            <label class="check-inline">
              <input v-model="webSearch" type="checkbox" />
              <strong>网页搜索</strong>
            </label>
            <p class="muted field-note">
              评估前用 Tavily 检索中报、分红与股权结构。无密钥时即使打开也不搜索。财务数字仍以已抓取年报为准。
            </p>

            <div class="migrate-box">
              <span class="muted">迁入路径：{{ yamlDir || '未配置' }}</span>
              <span class="badge" :class="migrated ? 'badge-pass' : 'badge-neutral'">{{ statusText }}</span>
            </div>
            <p v-if="error" class="alert-error">{{ error }}{{ detail ? `：${detail}` : '' }}</p>
          </div>
        </div>

        <div class="settings-actions">
          <button type="submit" class="btn-primary" :disabled="saving || !ready">
            {{ saving ? '保存中…' : '保存设置' }}
          </button>
          <span v-if="message" class="badge badge-pass">{{ message }}</span>
          <span v-if="formError" class="badge badge-fail">{{ formError }}</span>
        </div>
      </form>
    </template>
  </section>
</template>

<style scoped>
.settings-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}

.settings-card {
  margin: 0;
}

.label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.field-note {
  margin: -0.35rem 0 0;
  font-size: 0.82rem;
  line-height: 1.45;
}

.migrate-box {
  margin-top: 0.35rem;
  padding: 0.55rem 0.75rem;
  background: #faf8f3;
  border: 1px solid #e8e2d5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.85rem;
}

.settings-actions {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding-top: 0.25rem;
}

@media (max-width: 820px) {
  .settings-grid {
    grid-template-columns: 1fr;
  }
}
</style>
