<script setup lang="ts">
import { computed } from 'vue'
import { distribution, fmtScore } from '@/composables/useFit'

/**
 * A Choice answer drawn in full, winner on top.
 *
 * This component exists because of one real row: vacancy 40's top_gap is
 * "none" at confidence .17, with probabilities none .38 / seniority .26 /
 * domain .25 / technical .11. Printing the winner alone would read as
 * "no gap" when what the matcher actually said was "I can't tell".
 */
const props = defineProps<{
  probabilities: Record<string, number>
  labelFor: (key: string) => string
}>()

const rows = computed(() => distribution(props.probabilities, props.labelFor))
</script>

<template>
  <div class="dist">
    <div v-for="(r, i) in rows" :key="r.key" :class="['drow', { lead: i === 0 }]">
      <span class="dl">{{ r.label }}</span>
      <span class="dtrack"><span :style="{ width: Math.round(r.p * 100) + '%' }"></span></span>
      <span class="dp">{{ fmtScore(r.p) }}</span>
    </div>
  </div>
</template>

<style scoped>
.dist { display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.875rem; }
.drow { display: grid; grid-template-columns: 7rem minmax(0, 1fr) 2.25rem; gap: 0.625rem; align-items: center; }
.dl { font-size: 0.75rem; color: var(--p-text-muted-color); text-align: right; }
.drow.lead .dl { color: var(--p-text-color); font-weight: 600; }
.dtrack { height: 9px; background: var(--p-surface-200); border-radius: 4px; overflow: hidden; }
.dtrack > span { display: block; height: 100%; background: var(--p-surface-400); border-radius: 4px; }
.drow.lead .dtrack > span { background: var(--p-primary-color); }
.dp { font-size: 0.72rem; color: var(--p-text-muted-color); text-align: right; font-variant-numeric: tabular-nums; }
</style>
