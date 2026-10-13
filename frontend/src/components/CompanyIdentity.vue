<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import { api } from '@/api/client'
import type { CompanyIdentity as Identity } from '@/api/types'
import { host, isWeb, sameName } from '@/composables/useCompany'

/**
 * Who is behind a posting, as a model answers it: a second, paid source under
 * the hh profile. Per posting, not per company, since the posting is what
 * reveals an agency and its client.
 *
 * Shows only what the hh profile above does not already say.
 */
const props = defineProps<{ vacancyId: number; stored: Identity | null; employerName: string | null }>()

/** The stored answer until a new one lands; the parent's copy is not ours to edit. */
const identity = ref<Identity | null>(props.stored)
const error = ref<string | null>(null)
/** The vacancy a call runs for, not a boolean. Today the page remounts this
 *  card on every navigation, so the guard never fires; it matters only if the
 *  page ever keeps the card alive across vacancies. */
const pendingFor = ref<number | null>(null)

watch(() => [props.vacancyId, props.stored] as const, () => {
  identity.value = props.stored
  error.value = null
})

async function ask() {
  const id = props.vacancyId
  if (pendingFor.value === id) return
  pendingFor.value = id
  error.value = null
  try {
    const answer = await api.identifyCompany(id)
    if (props.vacancyId === id) identity.value = answer   // ignore a late answer for a page already left
  } catch (e) {
    if (props.vacancyId === id) error.value = (e as Error).message
  } finally {
    if (pendingFor.value === id) pendingFor.value = null
  }
}

/** A model's URL is linked only if it also answered a GET: one that did not
 *  may be invented. */
const linkable = (url: string | null, ok: boolean | null): url is string => isWeb(url) && ok === true

const behind = computed(() => {
  const employer = identity.value?.employer ?? null
  return employer && !sameName(employer, props.employerName) ? employer : null
})

const links = computed(() => {
  const i = identity.value
  if (!i) return []
  return ([['homepage', i.homepage, i.homepage_ok], ['careers', i.careers_page, i.careers_ok]] as const)
    .filter(([, url, ok]) => linkable(url, ok))
    .map(([label, url]) => ({ label, url: url as string }))
})

/** An answer that only repeats hh is still an answer: say so, rather than show nothing.
 *  "Direct employer" is something it added, so it does not count as nothing. */
const addsNothing = computed(() =>
  !behind.value && identity.value?.is_agency == null && links.value.length === 0)
</script>

<template>
  <div class="identity">
    <div class="ask">
      <Button :label="identity ? 'Ask again' : 'Ask a model who is behind it'" icon="pi pi-sparkles"
              text size="small" :loading="pendingFor === vacancyId" @click="ask" />
      <span class="note">about $0.0002 · 1–3 s · from what the model already knows</span>
    </div>

    <Message v-if="error" severity="warn" :closable="false">Could not ask the model ({{ error }}). Try again.</Message>

    <div v-if="identity" :title="identity.notes || undefined">
      <p v-if="behind" class="line">Behind it: <strong>{{ behind }}</strong></p>
      <p v-if="identity.is_agency" class="line">
        Recruiting agency —
        <template v-if="identity.client">hiring for <strong>{{ identity.client }}</strong></template>
        <span v-else class="muted">client not named</span>
      </p>
      <p v-else-if="identity.is_agency === false" class="line">Direct employer, as far as the model knows.</p>
      <p v-if="links.length" class="line">
        <template v-for="(link, n) in links" :key="link.label">
          <template v-if="n"> · </template>
          {{ link.label }} <a :href="link.url" target="_blank" rel="noopener noreferrer">{{ host(link.url) }} ↗</a>
        </template>
      </p>
      <p v-if="addsNothing" class="line muted">The model knows nothing beyond the hh profile.</p>
    </div>
  </div>
</template>

<style scoped>
/* A second answer from a second source: the divider marks the seam. */
.identity { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--p-content-border-color); }
.ask { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.5rem; }
.line { margin: 0; font-size: 0.95rem; line-height: 1.6; }
.line a { color: var(--p-primary-color); text-decoration: none; }
.line a:hover { text-decoration: underline; }
.muted { color: var(--p-text-muted-color); }
.note { font-size: 0.78rem; line-height: 1.5; color: var(--p-text-muted-color); }
</style>
