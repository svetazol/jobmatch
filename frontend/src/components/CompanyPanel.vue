<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import { api } from '@/api/client'
import type { Company } from '@/api/types'
import { host, hostPath, isWeb, sameName } from '@/composables/useCompany'
import AiButton from './AiButton.vue'
import RecruitersBlock from './RecruitersBlock.vue'

/**
 * The employer behind a vacancy, from its hh profile, looked up only when
 * asked. Stored per company, so another posting from an employer already
 * looked up shows it at once. The model's identity check is offered only to
 * fill a gap hh leaves: no website, or an agency whose client is unknown.
 */
const props = defineProps<{ vacancyId: number; companyName: string | null }>()

const company = ref<Company | null>(null)
const error = ref<string | null>(null)
/** The vacancy a lookup runs for, not a boolean: it matters only if the page
 *  ever keeps this section alive across vacancies. */
const pendingFor = ref<number | null>(null)
const identifyError = ref<string | null>(null)
const identifyingFor = ref<number | null>(null)

async function load(id: number) {
  company.value = null
  error.value = null
  identifyError.value = null
  try {
    const stored = await api.company(id)
    if (props.vacancyId === id) company.value = stored
  } catch { /* nothing stored is the same as not looked up: the action stays */ }
}

async function lookUp() {
  const id = props.vacancyId
  if (pendingFor.value === id) return
  pendingFor.value = id
  error.value = null
  try {
    const found = await api.lookUpCompany(id)
    if (props.vacancyId === id) company.value = found   // ignore a late answer for a page already left
  } catch (e) {
    if (props.vacancyId === id) error.value = (e as Error).message
  } finally {
    if (pendingFor.value === id) pendingFor.value = null
  }
}

watch(() => props.vacancyId, load, { immediate: true })

const AGENCY = 'Кадровое агентство'
const CATEGORY: Record<string, string> = { 'Организация': 'Organization', [AGENCY]: 'Recruiting agency' }

const noSite = computed(() => !!company.value && !isWeb(company.value.site_url))
const isAgency = computed(() => company.value?.category === AGENCY)

/** The gap the identity call would fill; null when hh already says it all. */
const askLabel = computed(() =>
  noSite.value && isAgency.value ? 'Identify'
    : noSite.value ? 'Find website'
      : isAgency.value ? 'Find client' : null)

/** ~$0.0002 and cached by its inputs, so asking twice is free. */
async function identify() {
  const id = props.vacancyId
  if (identifyingFor.value === id) return
  identifyingFor.value = id
  identifyError.value = null
  try {
    const answer = await api.identifyCompany(id)
    if (props.vacancyId === id && company.value) company.value.identity = answer   // ignore a late answer for a page already left
  } catch (e) {
    if (props.vacancyId === id) identifyError.value = (e as Error).message
  } finally {
    if (identifyingFor.value === id) identifyingFor.value = null
  }
}

const identity = computed(() => company.value?.identity ?? null)

/** A model's URL is linked only if it also answered a GET: one that did not may be invented. */
const modelHomepage = computed(() => {
  const i = identity.value
  return noSite.value && i?.homepage_ok === true && isWeb(i.homepage) ? i.homepage : null
})
const modelCareers = computed(() => {
  const i = identity.value
  return i?.careers_ok === true && isWeb(i.careers_page) ? i.careers_page : null
})

const realName = computed(() => {
  const employer = identity.value?.employer
  return employer && !sameName(employer, props.companyName) ? employer : null
})

/** hh's category or the model's answer: a client the model named is never hidden. */
const agencyLine = computed(() => !!identity.value && (isAgency.value || identity.value.is_agency === true))

/** A paid answer that adds nothing still says so, rather than show nothing. */
const addsNothing = computed(() =>
  !!identity.value && !modelHomepage.value && !modelCareers.value && !realName.value && !agencyLine.value)

/** What the LinkedIn link searches for. The website's host first: it names
 *  one company where a name is shared or translated (user, 2026-10-14); the
 *  homepage the model checked, else hh's. Without a site, the employer a model
 *  named, else the hh name; `||`, as an empty name is no name. */
const searchTerm = computed(() => {
  const c = company.value
  if (!c) return ''
  const site = c.identity?.homepage_ok === true && isWeb(c.identity.homepage) ? c.identity.homepage
    : isWeb(c.site_url) ? c.site_url : null
  return site ? host(site) : c.identity?.employer || c.name || ''
})

/** LinkedIn's company search: a company page lists its website, and its
 *  People tab is where the recruiters are (user, 2026-10-14). Quotes in a
 *  name (ООО «Ромашка») would close the phrase early, so they go. */
const linkedinSearch = computed(() => {
  const term = searchTerm.value.replace(/["«»]/g, '').trim()
  return 'https://www.linkedin.com/search/results/companies/?keywords=' + encodeURIComponent(`"${term}"`)
})

/** Industries and city stay in hh's words; only the category is translated. */
const details = computed(() => {
  const c = company.value
  if (!c) return ''
  const category = c.category ? CATEGORY[c.category] ?? c.category : null
  return [category, c.industries, c.city].filter(Boolean).join(' · ')
})
</script>

<template>
  <div class="company">
    <div class="sechead">
      <h2 class="label">Company</h2>
      <div class="acts">
        <Button :label="company ? 'Refresh' : 'Look up'" text size="small" severity="secondary"
                :loading="pendingFor === vacancyId" @click="lookUp" />
        <AiButton v-if="company && askLabel && !identity" :label="askLabel" price="$0.0002"
                  :loading="identifyingFor === vacancyId" loading-label="Checking…" @click="identify" />
      </div>
    </div>

    <p v-if="error" class="line warn">Couldn't look up ({{ error }})</p>

    <div v-if="company" class="body">
      <p v-if="realName" class="line" :title="identity?.notes || undefined">{{ realName }}</p>
      <p class="line links">
        <a v-if="isWeb(company.site_url)" :href="company.site_url" target="_blank" rel="noopener noreferrer">{{ host(company.site_url) }} ↗</a>
        <a v-if="modelHomepage" :title="identity?.notes || undefined" :href="modelHomepage" target="_blank" rel="noopener noreferrer"><i class="pi pi-sparkles" aria-hidden="true" />{{ host(modelHomepage) }} ↗</a>
        <a v-if="modelCareers" :title="identity?.notes || undefined" :href="modelCareers" target="_blank" rel="noopener noreferrer"><i class="pi pi-sparkles" aria-hidden="true" />{{ hostPath(modelCareers) }} ↗</a>
        <a v-if="isWeb(company.url)" :href="company.url" target="_blank" rel="noopener noreferrer">hh profile ↗</a>
        <!-- an empty term would search LinkedIn for every company there is -->
        <a v-if="searchTerm" :href="linkedinSearch" target="_blank" rel="noopener noreferrer">LinkedIn ↗</a>
      </p>
      <p v-if="agencyLine && identity" class="line muted" :title="identity.notes || undefined">
        Recruiting agency ·
        <template v-if="identity.client">hiring for <strong>{{ identity.client }}</strong></template>
        <template v-else>client not named</template>
        <template v-if="company.city"> · {{ company.city }}</template>
      </p>
      <p v-else-if="details" class="line muted">{{ details }}</p>
      <p v-if="addsNothing" class="line muted">Nothing found beyond hh</p>
      <p v-if="identifyError" class="line muted warn">Couldn't check ({{ identifyError }})</p>
    </div>
  </div>

  <RecruitersBlock v-if="company" :vacancy-id="vacancyId" :stored="company.recruiters" />
</template>

<style scoped>
.acts { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.body { margin-top: 0.5rem; display: flex; flex-direction: column; gap: 0.2rem; }
.line { margin: 0; font-size: 0.95rem; line-height: 1.6; }
.links { display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; }
a { color: var(--p-primary-color); text-decoration: none; }
a:hover { text-decoration: underline; }
a i { font-size: 0.7rem; margin-right: 0.25rem; }
.muted { font-size: 0.8rem; line-height: 1.45; color: var(--p-text-muted-color); }
.warn { font-size: 0.8rem; color: var(--status-warn); }
</style>
