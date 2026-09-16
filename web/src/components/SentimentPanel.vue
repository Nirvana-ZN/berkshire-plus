<script setup lang="ts">
const props = defineProps<{
  sentiment: {
    score: number | null
    zone?: string
    label: string
  } | null
}>()

function zoneClass(): string {
  const zone = props.sentiment?.zone
  if (zone === 'fear' || zone === 'greed' || zone === 'neutral') return zone
  return 'unknown'
}

function meterStyle(): Record<string, string> {
  const score = props.sentiment?.score
  if (score == null) return {}
  return { '--p': `${score}%`, '--n': String(score) }
}
</script>

<template>
  <section class="sentiment" :class="zoneClass()" :style="meterStyle()">
    <div class="sentiment-copy">
      <h2>情绪指数</h2>
      <p class="muted">0 恐惧 · 100 贪婪</p>
    </div>
    <p v-if="!sentiment || sentiment.score == null" class="sentiment-score">—</p>
    <template v-else>
      <p class="sentiment-score">{{ sentiment.score }}</p>
      <p class="sentiment-label">{{ sentiment.label }}</p>
      <div
        class="sentiment-meter"
        role="meter"
        :aria-valuenow="sentiment.score"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <i></i>
      </div>
    </template>
    <p v-if="sentiment?.zone === 'greed'" class="sentiment-warn">情绪贪婪：新评估不得写成通过。</p>
  </section>
</template>
