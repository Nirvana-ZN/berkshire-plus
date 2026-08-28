<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <div class="shell">
    <header>
      <p class="brand">本机工作台</p>
      <nav aria-label="主导航">
        <RouterLink to="/" active-class="is-prefix" exact-active-class="router-link-active">首页</RouterLink>
        <RouterLink to="/positions">看仓</RouterLink>
        <RouterLink to="/screen">选股</RouterLink>
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
