<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import Button from 'primevue/button'
import Toast from 'primevue/toast'
import { api } from '@/api/client'
import type { VacancyRow } from '@/api/types'

const dark = ref(false)

onMounted(() => {
  const saved = localStorage.getItem('theme')
  dark.value = saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches
  apply()
})

function apply() {
  document.documentElement.classList.toggle('dark', dark.value)
  localStorage.setItem('theme', dark.value ? 'dark' : 'light')
}
function toggle() { dark.value = !dark.value; apply() }

async function undoHide(row: VacancyRow) {
  row.hidden_at = null
  await api.triage(row.id, 'hidden', false)
}
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <RouterLink :to="{ name: 'list' }" class="brand">jobmatch</RouterLink>
      <nav class="nav">
        <RouterLink :to="{ name: 'list' }">Vacancies</RouterLink>
        <RouterLink :to="{ name: 'stats' }">Market view</RouterLink>
      </nav>
      <Button :icon="dark ? 'pi pi-sun' : 'pi pi-moon'" text rounded
              :aria-label="dark ? 'Switch to light theme' : 'Switch to dark theme'" @click="toggle" />
    </header>

    <main class="main"><RouterView /></main>

    <Toast group="undo" position="bottom-left">
      <template #message="{ message }">
        <div class="undo">
          <span>{{ message.summary }}</span>
          <Button label="Undo" size="small" @click="undoHide((message as any).row)" />
        </div>
      </template>
    </Toast>
  </div>
</template>

<style scoped>
.shell { min-height: 100vh; background: var(--p-surface-50); }
.topbar { display: flex; align-items: center; gap: 1.5rem; height: 3.5rem; padding: 0 1.5rem;
  background: var(--p-surface-0); border-bottom: 1px solid var(--p-surface-200); }
.brand { font-size: 1.05rem; font-weight: 700; letter-spacing: -0.01em; color: var(--p-text-color);
  text-decoration: none; }
.nav { display: flex; gap: 1rem; margin-right: auto; }
.nav a { font-size: 0.875rem; font-weight: 500; color: var(--p-text-muted-color); text-decoration: none; }
.nav a:hover { color: var(--p-text-color); }
.nav a.router-link-active { color: var(--p-text-color); font-weight: 600; }
.main { max-width: 1320px; margin: 0 auto; padding: 1.5rem 1rem 3.5rem; }
.undo { display: flex; align-items: center; gap: 1rem; }
</style>
