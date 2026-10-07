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
 */
const props = defineProps<{ vacancyId: number; companyName: string | null }>()

const company = ref<Company | null>(null)
const error = ref<string | null>(null)
const busy = ref(false)

async function load(id: number) {
  company.value = null
  error.value = null
  try {
    const stored = await api.company(id)
    if (props.vacancyId === id) company.value = stored
  } catch { /* nothing stored is the same as not looked up: the button stays */ }
}

async function lookUp() {
  if (busy.value) return
  const id = props.vacancyId
  busy.value = true
  error.value = null
  try {
    const found = await api.lookUpCompany(id)
    if (props.vacancyId === id) company.value = found   // ignore a late answer for a page already left
  } catch (e) {
    if (props.vacancyId === id) error.value = (e as Error).message
  } finally {
    busy.value = false
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

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
</script>

<template>
  <Card>
    <template #title>
      <div class="cardtop">
        <span>Company</span>
        <Button :label="company ? 'Refresh' : 'Look up company'" icon="pi pi-building"
                :text="!!company" size="small" :loading="busy" @click="lookUp" />
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
</style>
