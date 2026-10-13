<script setup lang="ts">
import { ref, watch } from 'vue'
import Card from 'primevue/card'
import Button from 'primevue/button'
import Message from 'primevue/message'
import { api } from '@/api/client'
import type { Company } from '@/api/types'

/**
 * The employer behind a vacancy, looked up only when asked.
 *
 * Stored per company, not per vacancy: opening another posting from an
 * employer already looked up shows it at once, with no request to hh.
 * The identity under it is per posting, since the posting is what reveals an
 * agency and its client.
 */
const props = defineProps<{ vacancyId: number; companyName: string | null }>()

const company = ref<Company | null>(null)
const error = ref<string | null>(null)
/** The vacancy a lookup runs for, not a boolean: a lookup left running on
 *  one page must not spin the button on the next. */
const pendingFor = ref<number | null>(null)
const step = ref<'hh' | 'model'>('hh')
const identifyError = ref<string | null>(null)

async function load(id: number) {
  company.value = null
  error.value = null
  identifyError.value = null
  try {
    const stored = await api.company(id)
    if (props.vacancyId === id) company.value = stored
  } catch { /* nothing stored is the same as not looked up: the button stays */ }
}

/** One button, two requests: hh first, shown as soon as it lands, then the
 *  model. A failed identity leaves the hh result in place. */
async function lookUp() {
  const id = props.vacancyId
  if (pendingFor.value === id) return
  pendingFor.value = id
  step.value = 'hh'
  error.value = null
  identifyError.value = null
  try {
    const found = await api.lookUpCompany(id)
    if (props.vacancyId !== id) return   // ignore a late answer for a page already left
    company.value = found
    await identify(id)
  } catch (e) {
    if (props.vacancyId === id) error.value = (e as Error).message
  } finally {
    if (pendingFor.value === id) pendingFor.value = null
  }
}

async function identify(id: number) {
  if (pendingFor.value === id) step.value = 'model'   // a newer lookup on another page owns `step`
  try {
    const identity = await api.identifyCompany(id)
    if (props.vacancyId === id && company.value) company.value = { ...company.value, identity }
  } catch (e) {
    if (props.vacancyId === id) identifyError.value = (e as Error).message
  }
}

watch(() => props.vacancyId, load, { immediate: true })

/** The host alone, which is what you would type; the link keeps the full URL. */
const host = (url: string) => {
  try { return new URL(url).host.replace(/^www\./, '') } catch { return url }
}

/** Only http(s) becomes a link: Vue renders a `javascript:` href as given.
 *  The API already drops those; this is the second lock on the same door. */
const isWeb = (url: string | null): url is string => !!url && /^https?:\/\//i.test(url)

/** A model's URL is linked only if it also answered a GET: one that did not
 *  may be invented. */
const linkable = (url: string | null, ok: boolean | null): url is string => isWeb(url) && ok === true

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })

/** Four decimals: one call is ~$0.0002, which two would round to nothing. */
const fmtCost = (usd: number | null) => usd === null ? 'cost unknown' : `$${usd.toFixed(4)}`
</script>

<template>
  <Card>
    <template #title>
      <div class="cardtop">
        <span>Company</span>
        <Button :label="company ? 'Refresh' : 'Look up company'" icon="pi pi-building"
                :text="!!company" size="small" :loading="pendingFor === vacancyId" @click="lookUp" />
      </div>
    </template>
    <template #content>
      <Message v-if="error" severity="warn" :closable="false">{{ error }}</Message>
      <template v-if="company">
        <p class="line">
          <strong>{{ company.name ?? companyName }}</strong>
          <template v-if="isWeb(company.site_url)">
            · <a :href="company.site_url" target="_blank" rel="noopener noreferrer">{{ host(company.site_url) }} ↗</a>
          </template>
          <span v-else class="muted"> · no website on its profile</span>
          · <a :href="company.url" target="_blank" rel="noopener noreferrer">hh profile ↗</a>
        </p>
        <p class="note">Looked up {{ fmtDate(company.looked_up_at) }}. The website is what the employer links from hh — sometimes a careers page.</p>

        <p v-if="pendingFor === vacancyId && step === 'model'" class="line who muted">
          <i class="pi pi-spin pi-spinner" /> Identifying… usually 1–3 seconds.
        </p>
        <template v-else-if="company.identity">
          <p class="line who">
            Who it is: <strong>{{ company.identity.employer ?? 'unknown' }}</strong>
            <template v-for="[label, url, ok] in ([
              ['homepage', company.identity.homepage, company.identity.homepage_ok],
              ['careers', company.identity.careers_page, company.identity.careers_ok],
            ] as const)" :key="label">
              · {{ label }}
              <a v-if="linkable(url, ok)" :href="url" target="_blank" rel="noopener noreferrer">{{ host(url) }} ↗</a>
              <template v-else-if="url">
                {{ host(url) }}<span v-if="ok === false" class="muted"> (did not answer)</span>
              </template>
              <span v-else class="muted">unknown</span>
            </template>
          </p>
          <p v-if="company.identity.is_agency" class="line">
            Recruiting agency —
            <template v-if="company.identity.client">hiring for <strong>{{ company.identity.client }}</strong></template>
            <span v-else class="muted">client not named</span>
          </p>
          <p v-else-if="company.identity.is_agency === null" class="line muted">Agency or direct employer: unknown</p>
          <p v-if="company.identity.notes" class="line muted">{{ company.identity.notes }}</p>
          <p class="note">
            {{ company.identity.model }} · {{ fmtCost(company.identity.cost_usd) }} · call #{{ company.identity.llm_call_id }}
          </p>
        </template>
        <p v-else-if="!identifyError" class="line who muted">Who it is: not identified yet — Refresh asks a model (~1–3 s, ~$0.0002).</p>

        <Message v-if="identifyError" severity="warn" :closable="false" class="who">
          Could not identify the employer: {{ identifyError }}
        </Message>
      </template>
      <p v-else-if="!error" class="note">
        {{ companyName ?? 'The employer' }} — its website and hh profile, read from hh when you ask.
      </p>
    </template>
  </Card>
</template>

<style scoped>
.cardtop { display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; }
.line { margin: 0; font-size: 0.95rem; line-height: 1.6; }
.line a { color: var(--p-primary-color); text-decoration: none; }
.line a:hover { text-decoration: underline; }
.muted { color: var(--p-text-muted-color); }
.note { margin: 0.875rem 0 0; font-size: 0.78rem; line-height: 1.5; color: var(--p-text-muted-color); }
.line + .note { margin-top: 0.5rem; }
/* The identity is a second answer from a second source: a gap marks the seam. */
.who { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--p-content-border-color); }
</style>
