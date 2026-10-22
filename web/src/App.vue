<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'

interface LockInfo {
  type?: string
  job_id?: number
}

const LOCK_LABEL: Record<string, string> = {
  funnel: '选股漏斗',
  research: '个股评估',
  review: '组合审视',
  discovery: '每周发现',
  discovery_data: '全市场抓取',
}

const lock = ref<LockInfo | null>(null)
let timer: number | undefined

const lockText = computed(() => {
  if (!lock.value) return '任务空闲'
  const kind = LOCK_LABEL[lock.value.type || ''] || lock.value.type || '后台任务'
  return `运行中：${kind}`
})

const lockLink = computed(() => {
  if (!lock.value) return '/'
  if (lock.value.type === 'research' && lock.value.job_id) {
    return `/research/${lock.value.job_id}`
  }
  if (lock.value.type === 'funnel') return '/screen'
  if (lock.value.type === 'review') return '/positions'
  if (lock.value.type === 'discovery' || lock.value.type === 'discovery_data') {
    return '/discover'
  }
  return '/'
})

async function pollHealth() {
  try {
    const res = await fetch('/api/health')
    if (res.ok) {
      const data = await res.json()
      lock.value = (data.lock as LockInfo | null) ?? null
    }
  } catch {
    // ignore transient poll errors in global header
  }
}

onMounted(() => {
  void pollHealth()
  timer = window.setInterval(() => {
    void pollHealth()
  }, 4000)
})

onBeforeUnmount(() => {
  if (timer != null) window.clearInterval(timer)
})
</script>

<template>
  <div class="shell">
    <header>
      <div class="brand-group">
        <p class="brand">本机工作台</p>
        <RouterLink
          v-if="lock"
          :to="lockLink"
          class="badge badge-warn nav-lock nav-lock-busy"
          title="点击跳转到正在运行的任务"
        >
          {{ lockText }} →
        </RouterLink>
        <span v-else class="badge badge-pass nav-lock">
          {{ lockText }}
        </span>
      </div>
      <nav aria-label="主导航">
        <RouterLink to="/" active-class="is-prefix" exact-active-class="router-link-active">首页</RouterLink>
        <RouterLink to="/positions">看仓</RouterLink>
        <RouterLink to="/screen">选股</RouterLink>
        <RouterLink to="/discover">每周发现</RouterLink>
        <RouterLink to="/index-funds">指数基金</RouterLink>
        <RouterLink to="/settings">设置</RouterLink>
      </nav>
    </header>
    <main>
      <RouterView />
    </main>
    <footer>
      <p>不构成投资建议</p>
    </footer>
  </div>
</template>

<style scoped>
.brand-group {
  display: flex;
  align-items: center;
  gap: 0.65rem;
}

.nav-lock {
  font-size: 0.75rem;
  text-decoration: none;
}

.nav-lock-busy {
  border: 1px solid #e6c9a2;
}
</style>
