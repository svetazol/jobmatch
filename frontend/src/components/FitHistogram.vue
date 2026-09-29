<script setup lang="ts">
import { computed } from 'vue'
import { FIT_ORDER, fmtScore } from '@/composables/useFit'
import { ramp } from '@/theme'

/**
 * The five stored probabilities, drawn. Deliberately NOT a Chart.js chart:
 * this renders once per list row, and a canvas per row would cost far more
 * than five divs for something 30px wide.
 */
const props = withDefaults(defineProps<{
  probabilities?: Record<string, number> | null
  label?: string | null
  confidence?: number | null
  size?: 'row' | 'panel'
}>(), { size: 'row' })

const bars = computed(() => {
  const colours = ramp()
  const probs = props.probabilities
  const values = FIT_ORDER.map((_, i) => probs?.[String(i)] ?? 0)
  const top = Math.max(...values)
  const maxH = props.size === 'row' ? 26 : 58
  return values.map((p, i) => ({
    level: FIT_ORDER[i]!,
    p,
    top: p > 0 && p === top,
    height: probs ? Math.max(2, Math.round(p * maxH)) : 2,
    colour: probs ? colours[i]! : 'var(--p-surface-300)',
  }))
})

// A low-confidence answer is shown faded: the shape is still there, the
// certainty it implies is not.
const faded = computed(() =>
  props.confidence !== null && props.confidence !== undefined && props.confidence < 0.5)
</script>

<template>
  <div :class="['hist', size, { faded }]" :aria-label="label ? `fit ${label}` : 'not matched yet'">
    <div v-for="b in bars" :key="b.level" class="hcol">
      <span v-if="size === 'panel'" class="hp">{{ fmtScore(b.p) }}</span>
      <span class="hb" :style="{ height: b.height + 'px', background: b.colour }"></span>
      <span v-if="size === 'panel'" :class="['hl', { top: b.top }]">{{ b.level }}</span>
    </div>
  </div>
</template>

<style scoped>
.hist { display: flex; align-items: flex-end; gap: 2px; }
.hist.row { height: 26px; width: 30px; flex: none; }
.hist.panel { gap: 9px; height: 92px; margin-top: 1rem; }
.hist.faded { opacity: 0.45; }
.hcol { display: flex; flex-direction: column; align-items: center; gap: 5px; }
.hist.row .hcol { width: 4px; }
.hist.panel .hcol { flex: 1; }
.hb { display: block; width: 100%; border-radius: 3px 3px 0 0; }
.hist.row .hb { border-radius: 1.5px; }
.hp { font-size: 11px; color: var(--p-text-muted-color); font-variant-numeric: tabular-nums; }
.hl { font-size: 11px; color: var(--p-text-muted-color); }
.hl.top { color: var(--p-text-color); font-weight: 600; }
</style>
