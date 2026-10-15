<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { CompanyRecruiters, RecruiterPerson } from '@/api/types'
import { isWeb } from '@/composables/useCompany'
import AiButton from './AiButton.vue'

/**
 * Recruiters, HR and talent acquisition at the company, from one paid AI web
 * search. Only people whose LinkedIn profile the search itself cited are
 * shown; the free LinkedIn link in the Company section is the way in without it.
 */
const props = defineProps<{ vacancyId: number; stored: CompanyRecruiters | null }>()

/** The stored search until a new one lands. */
const found = ref<CompanyRecruiters | null>(props.stored)
const error = ref<string | null>(null)
const markError = ref<string | null>(null)
/** The vacancy a search runs for, not a boolean: see CompanyPanel.vue. */
const pendingFor = ref<number | null>(null)
/** Profiles whose mark is being saved; a second click on one is ignored. */
const marking = new Set<string>()

watch(() => [props.vacancyId, props.stored] as const, () => {
  found.value = props.stored
  error.value = null
  markError.value = null
})

/** A shown search is searched again on purpose, so it always pays (refresh=true);
 *  a first click may get a recent stored search for free. */
async function search() {
  const id = props.vacancyId
  if (pendingFor.value === id) return
  pendingFor.value = id
  error.value = null
  try {
    const answer = await api.findRecruiters(id, { refresh: !!found.value })
    if (props.vacancyId === id) found.value = answer   // ignore a late answer for a page already left
  } catch (e) {
    if (props.vacancyId === id) error.value = (e as Error).message
  } finally {
    if (pendingFor.value === id) pendingFor.value = null
  }
}

/** Set by hand, as `applied` is; optimistic, rolled back if the save fails. */
async function toggleWritten(person: RecruiterPerson) {
  const url = person.linkedin_url
  if (marking.has(url)) return
  marking.add(url)
  const id = props.vacancyId
  const before = person.written_at
  const on = !before
  person.written_at = on ? new Date().toISOString() : null
  markError.value = null
  try { await api.markWritten(url, on) }
  catch (e) {
    person.written_at = before
    if (props.vacancyId === id) markError.value = (e as Error).message
  } finally {
    marking.delete(url)
  }
}

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })

/** `linkedin.com/in/<slug>`: the slug is what tells two namesakes apart. */
const slug = (url: string) => {
  try { return 'linkedin.com' + decodeURIComponent(new URL(url).pathname).replace(/\/$/, '') } catch { return url }
}

/** The page title is the search's own text, the check that the model's name matches the profile. */
const tip = (p: RecruiterPerson) => `${p.source_title ?? 'no page title'} · ${slug(p.linkedin_url)}`

const searchedOn = computed(() => found.value ? fmtDate(found.value.created_at) : '')
</script>

<template>
  <div class="recruiters">
    <div class="sechead">
      <h2 class="label">Recruiters</h2>
      <!-- ~$0.008 measured on the first two searches (2026-10-14, calls 19578-9) -->
      <AiButton :label="found ? 'Search again' : 'Find recruiters'" price="$0.01"
                :loading="pendingFor === vacancyId" loading-label="Searching… up to a minute" @click="search" />
    </div>

    <p v-if="error" class="muted warn">Search failed ({{ error }})</p>

    <template v-if="found">
      <div v-if="found.people.length" class="people">
        <template v-for="person in found.people" :key="person.linkedin_url">
          <a v-if="isWeb(person.linkedin_url)" class="name" :href="person.linkedin_url" :title="tip(person)"
             target="_blank" rel="noopener noreferrer">{{ person.name }} ↗</a>
          <span v-else class="name">{{ person.name }}</span>
          <span class="role">{{ person.role }}</span>
          <span class="mark">
            <button v-if="person.written_at" type="button" class="written" title="Click to undo"
                    @click="toggleWritten(person)">✓ Written {{ fmtDate(person.written_at) }}</button>
            <button v-else type="button" class="unwritten" @click="toggleWritten(person)">Mark as written</button>
          </span>
        </template>
      </div>
      <p v-if="markError" class="muted warn">Couldn't save the mark ({{ markError }})</p>
      <p v-if="found.people.length" class="muted">Searched {{ searchedOn }}</p>
      <p v-else class="muted">No profiles found · searched {{ searchedOn }}</p>
    </template>
  </div>
</template>

<style scoped>
/* a section of its own under Company: the parent's gap plus this margin matches the page's section spacing */
.recruiters { margin-top: 0.5rem; padding-top: 1.25rem; border-top: 1px solid var(--p-content-border-color);
  display: flex; flex-direction: column; gap: 0.5rem; }
.people { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr) auto;
  gap: 0.35rem 1.25rem; align-items: baseline; font-size: 0.95rem; line-height: 1.5; }
.name { font-weight: 600; overflow-wrap: anywhere; }
a { color: var(--p-primary-color); text-decoration: none; }
a:hover { text-decoration: underline; }
.mark { justify-self: end; }
.mark button { padding: 0; border: 0; background: none; font: inherit; font-size: 0.8rem; cursor: pointer; white-space: nowrap; }
.mark button:hover { text-decoration: underline; }
.written { color: var(--status-ok); }
.unwritten { color: var(--p-text-muted-color); }
.muted { margin: 0; font-size: 0.8rem; line-height: 1.45; color: var(--p-text-muted-color); }
.warn { color: var(--status-warn); }
</style>
