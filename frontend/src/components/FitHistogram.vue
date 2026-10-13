<script setup lang="ts">
import { computed } from 'vue'
import { FIT_ORDER, isVague } from '@/composables/useFit'
import { ramp } from '@/theme'

/**
 * The five stored probabilities, drawn. Deliberately NOT a Chart.js chart:
 * this renders once per list row, and a canvas per row would cost far more
 * than five divs for something 30px wide.
 */
const props = defineProps<{
  probabilities?: Record<string, number> | null
  label?: string | null
  confidence?: number | null
}>()

const bars = computed(() => {
  const colours = ramp()
  const probs = props.probabilities
  return FIT_ORDER.map((level, i) => ({
    level,
    height: probs ? Math.max(2, Math.round((probs[String(i)] ?? 0) * 26)) : 2,
    colour: probs ? colours[i]! : 'var(--p-surface-300)',
  }))
})

// A low-confidence answer is shown faded: the shape is still there, the
// certainty it implies is not.
const faded = computed(() => isVague(props.confidence))
</script>

<template>
  <div :class="['hist', { faded }]" :aria-label="label ? `fit ${label}` : 'not matched yet'">
    <span v-for="b in bars" :key="b.level" class="hb" :style="{ height: b.height + 'px', background: b.colour }"></span>
  </div>
</template>

<style scoped>
.hist { display: flex; align-items: flex-end; gap: 2px; height: 26px; width: 30px; flex: none; }
.hist.faded { opacity: 0.45; }
.hb { display: block; width: 4px; border-radius: 1.5px; }
</style>
