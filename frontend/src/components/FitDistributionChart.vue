<script setup lang="ts">
import { computed } from 'vue'
import Chart from 'primevue/chart'
import type { FitLevel } from '@/api/types'
import { FIT_ORDER } from '@/composables/useFit'
import { neutral, ramp } from '@/theme'

const props = defineProps<{ distribution: Record<FitLevel, number> }>()

const data = computed(() => ({
  labels: [...FIT_ORDER],
  datasets: [{
    // one series, so no legend: the x-axis already names every bar
    label: 'postings',
    data: FIT_ORDER.map((l) => props.distribution[l]),
    backgroundColor: ramp() as unknown as string[],
    borderRadius: 4,
    borderSkipped: false,
  }],
}))

const options = computed(() => ({
  maintainAspectRatio: false,
  responsive: true,
  plugins: { legend: { display: false } },
  scales: {
    x: { grid: { display: false }, border: { display: false } },
    y: { beginAtZero: true, grid: { color: neutral().rule, drawTicks: false }, border: { display: false } },
  },
}))
</script>

<template>
  <Chart type="bar" :data="data" :options="options" class="fit-chart" />
</template>

<style scoped>
.fit-chart { height: 260px; }
</style>
