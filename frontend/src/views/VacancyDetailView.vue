<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Chip from 'primevue/chip'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import { api } from '@/api/client'
import type { CoverNote, VacancyDetail } from '@/api/types'
import { isWeb } from '@/composables/useCompany'
import AiButton from '@/components/AiButton.vue'
import CompanyPanel from '@/components/CompanyPanel.vue'
import VerdictCard from '@/components/VerdictCard.vue'

const route = useRoute()
const router = useRouter()
const v = ref<VacancyDetail | null>(null)
const error = ref<string | null>(null)

async function load(id: number) {
  error.value = null
  v.value = null
  note.value = null
  noteError.value = null
  try { v.value = await api.vacancy(id) }
  catch (e) { error.value = (e as Error).message }
  if (v.value) markSeen(v.value)
}

/**
 * Opening a vacancy is what "seen" means — nothing else ever set it, so
 * `seen_at` stayed null for the whole corpus and the list's "Unseen only"
 * filter quietly returned everything.
 *
 * Deliberately not awaited and deliberately swallowed: this is a side effect
 * of reading, and a failed PATCH must not put an error banner over a page
 * that loaded fine. The list picks the change up on its own — it is not kept
 * alive, so going back re-mounts it and refetches.
 */
const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })

/** Each fact once, in one line; 69% of matched postings state no salary (2026-10-13), so it says so. */
const meta = computed(() => {
  if (!v.value) return ''
  const { company, country, work_formats, experience_raw, salary_raw, published_at } = v.value
  return [company, country, work_formats.join(', '), experience_raw, salary_raw || 'salary not stated',
    published_at && `posted ${fmtDate(published_at)}`].filter(Boolean).join(' · ')
})

/** Set by hand: nothing here can see your hh.ru account. */
async function toggleApplied() {
  if (!v.value) return
  const on = !v.value.applied_at
  v.value.applied_at = on ? new Date().toISOString() : null
  try { await api.triage(v.value.id, 'applied', on) }
  catch (e) {
    if (v.value) v.value.applied_at = on ? null : new Date().toISOString()
    error.value = (e as Error).message
  }
}

function markSeen(row: VacancyDetail) {
  if (row.seen_at) return
  row.seen_at = new Date().toISOString()
  api.triage(row.id, 'seen', true).catch(() => { /* a read, not a promise */ })
}
/**
 * Drafted on demand by `claude -p` on the API's machine and never stored:
 * leaving the page drops it, and the button writes a fresh one.
 */
const note = ref<CoverNote | null>(null)
const noteError = ref<string | null>(null)
const noteBusy = ref(false)
const copied = ref(false)

async function writeNote() {
  if (!v.value || noteBusy.value) return
  const id = v.value.id
  noteBusy.value = true
  noteError.value = null
  try {
    const result = await api.coverNote(id)
    if (v.value?.id === id) note.value = result   // ignore a late answer for a page already left
  } catch (e) {
    if (v.value?.id === id) noteError.value = (e as Error).message
  } finally {
    noteBusy.value = false
  }
}

async function copyNote() {
  if (!note.value) return
  await navigator.clipboard.writeText(note.value.text)
  copied.value = true
  setTimeout(() => { copied.value = false }, 1500)
}

onMounted(() => load(Number(route.params.id)))
watch(() => route.params.id, (id) => load(Number(id)))
</script>

<template>
  <section class="page">
    <nav class="crumb">
      <Button icon="pi pi-chevron-left" label="All matches" text @click="router.push({ name: 'list' })" />
    </nav>

    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>
    <Skeleton v-if="!v && !error" height="20rem" border-radius="14px" />

    <template v-if="v">
      <!-- only when the posting may be gone: an hh 404 is proof, absence from search is a hint -->
      <Message v-if="v.delisted_at" severity="warn" :closable="false">
        Closed on hh.ru on {{ fmtDate(v.delisted_at) }}.
      </Message>
      <Message v-else-if="v.stale" severity="secondary" :closable="false">
        Not seen in hh search since {{ fmtDate(v.last_seen_at) }} — it may be closed.
      </Message>

      <header class="head">
        <div class="titlebox">
          <h1>{{ v.title }}</h1>
          <p class="meta">{{ meta }}</p>
        </div>
        <div class="acts">
          <Button :label="v.applied_at ? `Applied ${fmtDate(v.applied_at)}` : 'Mark applied'"
                  :icon="v.applied_at ? 'pi pi-check-circle' : 'pi pi-send'"
                  :severity="v.applied_at ? 'success' : 'secondary'" outlined size="small"
                  :title="v.applied_at ? 'Click to undo' : undefined" @click="toggleApplied" />
          <Button v-if="isWeb(v.url)" as="a" :href="v.url" target="_blank" rel="noopener noreferrer"
                  label="hh.ru ↗" severity="secondary" outlined size="small" />
        </div>
      </header>

      <section class="sec">
        <VerdictCard :match="v.match" :answers="v.answers" :seniority="v.seniority" />
      </section>

      <section class="sec">
        <CompanyPanel :vacancy-id="v.id" :company-name="v.company" />
      </section>

      <section class="sec">
        <div class="sechead">
          <div>
            <h2 class="label">Cover letter</h2>
            <p v-if="note" class="muted">In {{ note.language }}. A draft: check every claim before sending.</p>
            <p v-else class="muted">From your CV, in the posting's language</p>
          </div>
          <div class="acts">
            <Button v-if="note" :label="copied ? 'Copied' : 'Copy'" :icon="copied ? 'pi pi-check' : 'pi pi-copy'"
                    text size="small" severity="secondary" @click="copyNote" />
            <AiButton :label="note ? 'Rewrite' : 'Write draft'" :loading="noteBusy" loading-label="Writing… 10–30 s"
                      title="Claude drafts it on this machine; 10–30 s" @click="writeNote" />
          </div>
        </div>
        <Message v-if="noteError" severity="error" :closable="false">{{ noteError }}</Message>
        <p v-if="note" class="covernote" :class="{ busy: noteBusy }">{{ note.text }}</p>
      </section>

      <section class="sec">
        <h2 class="label">Posting</h2>
        <div v-if="v.skills.length" class="skills">
          <Chip v-for="s in v.skills" :key="s" :label="s" />
        </div>
        <!-- plain text with list bullets already lost in scraping -->
        <pre v-if="v.description" class="desc">{{ v.description }}</pre>
      </section>
    </template>
  </section>
</template>

<style scoped>
/* one quiet column: no cards, sections split by a hairline */
.page { display: flex; flex-direction: column; gap: 0.875rem; max-width: 56rem; margin: 0 auto; }
.crumb { margin-bottom: -0.25rem; }
.head { display: flex; gap: 1.25rem; align-items: flex-start; flex-wrap: wrap; }
.titlebox { flex: 1 1 20rem; min-width: 0; }
h1 { margin: 0; font-size: 1.5rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.25; }
.meta { margin: 0.5rem 0 0; font-size: 0.875rem; color: var(--p-text-muted-color); }
.acts { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.sec { display: flex; flex-direction: column; gap: 0.75rem; padding-top: 1.25rem;
  border-top: 1px solid var(--p-content-border-color); }
.sechead { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; }
.label { margin: 0; font-size: 10.5px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--p-text-muted-color); }
.muted { margin: 0.25rem 0 0; font-size: 0.8rem; line-height: 1.45; color: var(--p-text-muted-color); }
.skills { display: flex; flex-wrap: wrap; gap: 0.45rem; }
.desc { margin: 0; font-family: inherit; font-size: 0.85rem; line-height: 1.65; white-space: pre-wrap; }
.covernote { margin: 0; font-size: 0.95rem; line-height: 1.65; white-space: pre-wrap; }
.covernote.busy { opacity: 0.5; }
</style>
