<script setup lang="ts">
import { ref } from 'vue'

/** A vacancy-page section whose body can be closed; the choice is remembered per section, on this browser only. */
const props = defineProps<{ label: string; name: string }>()

const KEY = `fold:${props.name}`
// storage can throw (private window, blocked site data): the section then just starts open
function read(): boolean {
  try { return localStorage.getItem(KEY) !== 'closed' } catch { return true }
}
const open = ref(read())

function toggle() {
  open.value = !open.value
  try { localStorage.setItem(KEY, open.value ? 'open' : 'closed') } catch { /* a convenience, not state */ }
}
</script>

<template>
  <div class="fold">
    <div class="sechead">
      <h2 class="label">
        <button type="button" class="toggle" :aria-expanded="open" @click="toggle">
          <i class="pi" :class="open ? 'pi-chevron-down' : 'pi-chevron-right'" aria-hidden="true" />{{ label }}
        </button>
      </h2>
      <div class="acts"><slot name="actions" /></div>
    </div>
    <div v-show="open" class="body"><slot /></div>
  </div>
</template>

<style scoped>
.toggle { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0; border: 0; background: none;
  font: inherit; font-size: 11.5px; font-weight: 700; letter-spacing: inherit; text-transform: inherit;
  color: var(--p-text-color); cursor: pointer; }
.toggle:hover { color: var(--p-primary-color); }
.toggle i { font-size: 0.6rem; }
.acts { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.body { margin-top: 0.75rem; display: flex; flex-direction: column; gap: 0.75rem; }
</style>
