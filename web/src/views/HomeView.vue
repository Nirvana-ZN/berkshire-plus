<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <section>
    <h1>工作台</h1>
    <p>从看仓或选股开始本周流程。</p>
    <div class="entries">
      <article>
        <h2><RouterLink to="/positions">看仓</RouterLink></h2>
        <p>查看持仓入口。</p>
      </article>
      <article>
        <h2><RouterLink to="/screen">选股</RouterLink></h2>
        <p>进入选股入口。</p>
      </article>
    </div>
  </section>
</template>
