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
const model = ref('')
const baseUrl = ref('')
const webSearch = ref(false)
const apiKey = ref('')
const saving = ref(false)
const ready = ref(false)

const statusText = computed(() => (migrated.value ? '已迁入' : '未迁入'))

function applySettings(data: Record<string, unknown>) {
  yamlDir.value = (data.yaml_dir as string | null) ?? null
  migrated.value = Boolean(data.migrated)
  hasApiKey.value = Boolean(data.has_api_key)
  model.value = typeof data.llm_model === 'string' ? data.llm_model : ''
  baseUrl.value = typeof data.llm_base_url === 'string' ? data.llm_base_url : ''
  webSearch.value = Boolean(data.web_search)
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
    <h1>设置</h1>
    <p v-if="loadError">{{ loadError }}</p>
    <template v-else-if="ready">
      <form class="stack-form" @submit.prevent="saveSettings">
        <label>
          API 密钥
          <input v-model="apiKey" type="password" autocomplete="off" />
        </label>
        <p v-if="hasApiKey" class="muted">已保存</p>
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
        <p class="muted">智谱 Coding Plan 用 /api/coding/paas/v4；按量计费用 /api/paas/v4</p>
        <label class="check-inline">
          <input v-model="webSearch" type="checkbox" />
          网页搜索
        </label>
        <button type="submit" :disabled="saving || !ready">保存</button>
      </form>
      <p v-if="formError">{{ formError }}</p>
      <p v-if="message">{{ message }}</p>
      <p class="hint">迁入路径：{{ yamlDir || '未配置' }}　{{ statusText }}</p>
      <p v-if="error">{{ error }}{{ detail ? `：${detail}` : '' }}</p>
    </template>
  </section>
</template>
