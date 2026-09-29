<script setup lang="ts">
import { computed } from 'vue'
import Chart from 'primevue/chart'
import type { PitchStat } from '@/api/types'
import { FIT_ORDER, PITCH_LABEL } from '@/composables/useFit'
import { neutral, ramp } from '@/theme'

const props = defineProps<{ pitches: PitchStat[] }>()

/** "Other" is not a pitch — it is the discard pile, so it sorts last no
 *  matter how big it gets (it is currently the biggest bucket at 71). */
const ordered = computed(() => {
  const real = props.pitches.filter((p) => p.pitch !== 'none')
    .sort((a, b) => b.mean_fit - a.mean_fit)
  const other = props.pitches.filter((p) => p.pitch === 'none')
  return [...real, ...other]
})

const data = computed(() => {
  const colours = ramp()
  return {
    labels: ordered.value.map((p) => PITCH_LABEL[p.pitch]),
    datasets: FIT_ORDER.map((level, i) => ({
      label: level,
      backgroundColor: colours[i]!,
      // the 2px surface gap between segments, done with a border in the
      // surface colour rather than a stroke around each bar
      borderColor: neutral().surface,
      borderWidth: { top: 0, bottom: 0, left: 1, right: 1 },
      borderSkipped: false,
      borderRadius: 4,
      data: ordered.value.map((p) => p.distribution[i] ?? 0),
    })),
  }
})

const options = computed(() => ({
  indexAxis: 'y' as const,
  maintainAspectRatio: false,
  responsive: true,
  plugins: {
    legend: {
      position: 'top' as const,
      align: 'end' as const,
      labels: { boxWidth: 11, boxHeight: 11, usePointStyle: false, padding: 14 },
    },
    tooltip: {
      callbacks: {
        // the caption a person actually needs: share of the lane, not a bare count
        label: (ctx: any) => {
          const stat = ordered.value[ctx.dataIndex]!
          const n = ctx.raw as number
          return `${ctx.dataset.label}: ${n} of ${stat.n} (${Math.round((n / stat.n) * 100)}%)`
        },
      },
    },
  },
  scales: {
    x: {
      stacked: true,
      grid: { color: neutral().rule, drawTicks: false },
      border: { display: false },
      title: { display: true, text: 'postings' },
    },
    y: { stacked: true, grid: { display: false }, border: { display: false } },
  },
}))
</script>

<template>
  <Chart type="bar" :data="data" :options="options" class="pitch-chart" />
</template>

<style scoped>
.pitch-chart { height: 260px; }
</style>
