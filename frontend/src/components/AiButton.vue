<script setup lang="ts">
/**
 * The one look for a button that asks a model: a pill with sparkles and,
 * when it costs money, the price inside it. While busy it greys out and its
 * label becomes the wait, so the user knows the request is running.
 * `title` (the timing) and `@click` fall through to the <button>. It is not
 * disabled while busy, so focus stays on it; each caller ignores a second click.
 */
defineProps<{
  label: string
  /** e.g. "$0.01"; omitted when the call is free to the user. */
  price?: string
  loading?: boolean
  /** What the label says while the request runs, e.g. "Writing… 10–30 s". */
  loadingLabel?: string
}>()
</script>

<template>
  <button type="button" :class="['ai', { busy: loading }]" :aria-busy="loading">
    <i :class="loading ? 'pi pi-spin pi-spinner' : 'pi pi-sparkles'" aria-hidden="true" />
    <span>{{ loading && loadingLabel ? loadingLabel : label }}</span>
    <span v-if="price && !loading" class="price">· {{ price }}</span>
  </button>
</template>

<style scoped>
.ai { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.3rem 0.8rem;
  border: 1px solid var(--p-primary-color); border-radius: 999px;
  background: color-mix(in srgb, var(--p-primary-color) 8%, transparent);
  color: var(--p-primary-color); font: inherit; font-size: 0.82rem; font-weight: 500;
  line-height: 1.4; white-space: nowrap; cursor: pointer; }
.ai:not(.busy):hover { background: color-mix(in srgb, var(--p-primary-color) 14%, transparent); }
.ai:focus-visible { outline: 2px solid var(--p-primary-color); outline-offset: 2px; }
.ai i { font-size: 0.8rem; }
.price { font-size: 0.75rem; color: var(--p-text-muted-color); }
.ai.busy { border-color: var(--p-surface-300); background: var(--p-surface-100);
  color: var(--p-text-muted-color); cursor: progress; }
</style>
