<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import { api } from '@/api/client'
import type { CompanyRecruiters } from '@/api/types'
import { isWeb } from '@/composables/useCompany'

/**
 * Recruiters, HR and talent acquisition at the company, from one paid AI web
 * search. Only people whose LinkedIn profile the search itself cited are
 * shown; the free search link beside it is always there, so the paid one is
 * a shortcut, never the only way in.
 */
const props = defineProps<{ vacancyId: number; stored: CompanyRecruiters | null; searchTerm: string }>()

/** The stored search until a new one lands; the parent's copy is not ours to edit. */
const found = ref<CompanyRecruiters | null>(props.stored)
const error = ref<string | null>(null)
/** The vacancy a search runs for, not a boolean: see CompanyIdentity.vue. */
const pendingFor = ref<number | null>(null)

watch(() => [props.vacancyId, props.stored] as const, () => {
  found.value = props.stored
  error.value = null
})

/** `refresh` asks for a new paid search; without it the API may return a
 *  recent one for free. */
async function search(refresh = false) {
  const id = props.vacancyId
  if (pendingFor.value === id) return
  pendingFor.value = id
  error.value = null
  try {
    const answer = await api.findRecruiters(id, { refresh })
    if (props.vacancyId === id) found.value = answer   // ignore a late answer for a page already left
  } catch (e) {
    if (props.vacancyId === id) error.value = (e as Error).message
  } finally {
    if (pendingFor.value === id) pendingFor.value = null
  }
}

/** LinkedIn's company search for the website (or the name without one, see
 *  CompanyPanel.vue): a company page lists its website, and its People tab is
 *  where the recruiters are. The user's choice, 2026-10-14. Quotes in a name
 *  (ООО «Ромашка») would close the phrase early, so they go. */
const linkedinSearch = computed(() => {
  const term = props.searchTerm.replace(/["«»]/g, '').trim()
  return 'https://www.linkedin.com/search/results/companies/?keywords='
    + encodeURIComponent(`"${term}"`)
})

/** `linkedin.com/in/<slug>`: the slug is what tells two namesakes apart. */
const profile = (url: string) => {
  try { return 'linkedin.com' + decodeURIComponent(new URL(url).pathname).replace(/\/$/, '') } catch { return url }
}

const searchedOn = computed(() => found.value
  ? new Date(found.value.created_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
  : '')
</script>

<template>
  <div class="recruiters">
    <p class="head">
      Recruiters
      <!-- An empty term would search LinkedIn for every recruiter there is. -->
      <a v-if="searchTerm" class="free" :href="linkedinSearch" target="_blank" rel="noopener noreferrer">Find the company on LinkedIn ↗</a>
    </p>

    <!-- ~$0.008 measured on the first two searches (2026-10-14, calls 19578-9) -->
    <!-- A shown search is refreshed on purpose: refresh=true always pays for a new one. -->
    <div v-if="found" class="ask">
      <span class="note">Searched {{ searchedOn }} ·</span>
      <Button label="Refresh" icon="pi pi-refresh" text size="small"
              :loading="pendingFor === vacancyId" @click="search(true)" />
      <span v-if="pendingFor === vacancyId" class="note">Searching the web… up to a minute.</span>
      <span v-else class="note">about $0.01 — runs a new search</span>
    </div>
    <div v-else class="ask">
      <Button label="Find recruiters" icon="pi pi-search"
              text size="small" :loading="pendingFor === vacancyId" @click="search()" />
      <span v-if="pendingFor === vacancyId" class="note">Searching the web… up to a minute.</span>
      <span v-else class="note">about $0.01 · a few seconds · an AI web search</span>
    </div>

    <Message v-if="error" severity="warn" :closable="false">Could not search ({{ error }}). Try again.</Message>

    <template v-if="found">
      <div v-for="person in found.people" :key="person.linkedin_url" class="person">
        <p class="line">
          <strong>{{ person.name }}</strong>
          <template v-if="person.role"> · {{ person.role }}</template>
          <template v-if="isWeb(person.linkedin_url)">
            · <a :href="person.linkedin_url" target="_blank" rel="noopener noreferrer">{{ profile(person.linkedin_url) }} ↗</a>
          </template>
        </p>
        <!-- Only the page title comes from the search itself; the name, role and
             note above are the model's. The title is the check that they match. -->
        <p class="source">
          <template v-if="person.source_title">LinkedIn: {{ person.source_title }}</template>
          <span v-else class="muted">LinkedIn: page title not returned</span>
        </p>
        <p class="snippet">model's note: <em>{{ person.evidence }}</em></p>
      </div>

      <template v-if="!found.people.length">
        <p class="line muted">No profiles the search could confirm.</p>
        <p v-if="found.sources === 0" class="line muted">The search returned no sources to check against.</p>
      </template>

      <p class="note summary">
        {{ found.people.length }} found
        <template v-if="found.dropped > 0">
          · <span class="dropped" title="named by the model but not among the links the search returned">{{ found.dropped }} dropped</span>
        </template>
      </p>
    </template>
  </div>
</template>

<style scoped>
/* A third source, the open web: the divider marks the seam, as above it. */
.recruiters { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--p-content-border-color); }
.head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; margin: 0 0 0.25rem; font-weight: 600; }
.free { font-size: 0.85rem; font-weight: normal; }
.ask { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.5rem; }
.person { margin-bottom: 0.5rem; }
.line { margin: 0; font-size: 0.95rem; line-height: 1.6; }
a { color: var(--p-primary-color); text-decoration: none; }
a:hover { text-decoration: underline; }
.source { margin: 0; font-size: 0.85rem; line-height: 1.45; }
.snippet { margin: 0; font-size: 0.8rem; line-height: 1.45; color: var(--p-text-muted-color); }
.muted { color: var(--p-text-muted-color); }
.note { font-size: 0.78rem; line-height: 1.5; color: var(--p-text-muted-color); }
.summary { margin: 0.25rem 0 0; }
.dropped { text-decoration: underline dotted; cursor: help; }
</style>
