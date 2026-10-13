<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Card from 'primevue/card'
import Tag from 'primevue/tag'
import Chip from 'primevue/chip'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import { api } from '@/api/client'
import type { CoverNote, VacancyDetail } from '@/api/types'
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

      <Card>
        <template #content>
          <div class="topline">
            <div class="titlebox">
              <h1>{{ v.title }}</h1>
              <p class="org">
                {{ v.company }} · {{ v.country }} · {{ v.work_formats.join(', ') }}<template
                  v-if="v.published_at"> · posted {{ fmtDate(v.published_at) }}</template>
              </p>
            </div>
            <div class="topacts">
              <Button :label="v.applied_at ? `Applied ${fmtDate(v.applied_at)}` : 'Mark as applied'"
                      :icon="v.applied_at ? 'pi pi-check-circle' : 'pi pi-send'"
                      :severity="v.applied_at ? 'success' : 'secondary'" :outlined="!v.applied_at"
                      :title="v.applied_at ? 'Click to undo' : undefined" @click="toggleApplied" />
              <a :href="v.url" target="_blank" rel="noopener">
                <Button label="Open on hh.ru" icon="pi pi-external-link" icon-pos="right" />
              </a>
            </div>
          </div>
          <div class="facts">
            <Tag v-if="v.experience_raw" :value="v.experience_raw" severity="secondary" />
            <!-- 69% of matched postings state no salary (2026-10-13), so absent is the normal case -->
            <Tag v-if="v.salary_raw" :value="v.salary_raw" severity="secondary" />
            <span v-else class="nosalary">salary not stated</span>
          </div>
        </template>
      </Card>

      <VerdictCard :match="v.match" :answers="v.answers" />

      <CompanyPanel :vacancy-id="v.id" :company-name="v.company" />

      <Card>
        <template #title>
          <div class="cardtop">
            <span>Cover letter</span>
            <div class="noteactions">
              <Button v-if="note" :label="copied ? 'Copied' : 'Copy'" :icon="copied ? 'pi pi-check' : 'pi pi-copy'"
                      text size="small" @click="copyNote" />
              <Button :label="note ? 'Rewrite' : 'Write cover letter'" icon="pi pi-sparkles" size="small"
                      :loading="noteBusy" @click="writeNote" />
            </div>
          </div>
        </template>
        <template #content>
          <Message v-if="noteError" severity="error" :closable="false">{{ noteError }}</Message>
          <p v-if="noteBusy && !note" class="note">Asking Claude — this takes 10–30 seconds.</p>
          <p v-if="note" class="covernote" :class="{ busy: noteBusy }">{{ note.text }}</p>
          <p v-if="note" class="note">
            In {{ note.language }}, from the CV in config.toml. A draft: check every claim before sending.
          </p>
          <p v-if="!note && !noteBusy && !noteError" class="note">
            A short cover letter built from the CV, in the posting's language.
          </p>
        </template>
      </Card>

      <Card>
        <template #title>The posting</template>
        <template #content>
          <div v-if="v.skills.length" class="skills">
            <Chip v-for="s in v.skills" :key="s" :label="s" />
          </div>
          <!-- plain text with list bullets already lost in scraping -->
          <pre v-if="v.description" class="desc">{{ v.description }}</pre>
        </template>
      </Card>
    </template>
  </section>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 0.875rem; }
.crumb { margin-bottom: -0.25rem; }
.topacts { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.topline { display: flex; gap: 1.25rem; align-items: flex-start; flex-wrap: wrap; }
.titlebox { flex: 1 1 20rem; min-width: 0; }
h1 { margin: 0; font-size: 1.5rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.25; }
.org { margin: 0.5rem 0 0; font-size: 0.875rem; color: var(--p-text-muted-color); }
.facts { margin-top: 1rem; display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.nosalary { font-size: 0.78rem; font-style: italic; color: var(--p-text-muted-color); }
.cardtop { display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; }
.note { margin: 0.875rem 0 0; font-size: 0.78rem; line-height: 1.5; color: var(--p-text-muted-color); }
.skills { display: flex; flex-wrap: wrap; gap: 0.45rem; }
.skills + .desc { margin-top: 1rem; }
.desc { margin: 0; font-family: inherit; font-size: 0.85rem; line-height: 1.65; white-space: pre-wrap; }
.noteactions { display: flex; gap: 0.25rem; align-items: center; }
.covernote { margin: 0; font-size: 0.95rem; line-height: 1.65; white-space: pre-wrap; }
.covernote.busy { opacity: 0.5; }
</style>
