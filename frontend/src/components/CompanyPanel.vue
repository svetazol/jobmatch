<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Card from 'primevue/card'
import Button from 'primevue/button'
import Message from 'primevue/message'
import { api } from '@/api/client'
import type { Company } from '@/api/types'
import { host, isWeb, sameName } from '@/composables/useCompany'
import CompanyIdentity from './CompanyIdentity.vue'
import RecruitersBlock from './RecruitersBlock.vue'

/**
 * The employer behind a vacancy, as its hh profile describes it, looked up
 * only when asked.
 *
 * Stored per company, not per vacancy: opening another posting from an
 * employer already looked up shows it at once, with no request to hh.
 * The model's answer under it is a separate, paid request with its own button,
 * and so is the recruiter search under that.
 */
const props = defineProps<{ vacancyId: number; companyName: string | null }>()

const company = ref<Company | null>(null)
const error = ref<string | null>(null)
/** The vacancy a lookup runs for, not a boolean. Today the page remounts this
 *  card on every navigation, so the guard never fires; it matters only if the
 *  page ever keeps the card alive across vacancies. */
const pendingFor = ref<number | null>(null)

async function load(id: number) {
  company.value = null
  error.value = null
  try {
    const stored = await api.company(id)
    if (props.vacancyId === id) company.value = stored
  } catch { /* nothing stored is the same as not looked up: the button stays */ }
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

/** The name the paid search uses, so the free link asks the same: the employer
 *  behind the posting when a model has named it (an agency's hh name finds the
 *  agency's recruiters), else the hh name. `||`, as an empty name is no name.
 *  The stored answer only: one asked just now shows on reload. */
const searchName = computed(() => company.value?.identity?.employer || company.value?.name || '')

/** hh's own words, in its language: translating them would add nothing but doubt. */
const details = computed(() => {
  const c = company.value
  return c ? [c.category, c.city, c.industries].filter(Boolean).join(' · ') : ''
})
</script>

<template>
  <Card>
    <template #title>
      <div class="cardtop">
        <span>Company</span>
        <Button :label="company ? 'Refresh from hh' : 'Look up company'" icon="pi pi-building"
                :text="!!company" size="small" :loading="pendingFor === vacancyId" @click="lookUp" />
      </div>
    </template>
    <template #content>
      <Message v-if="error" severity="warn" :closable="false">{{ error }}</Message>
      <template v-if="company">
        <p class="line">
          <template v-if="company.name && !sameName(company.name, companyName)">
            <strong>{{ company.name }}</strong> ·
          </template>
          <a v-if="isWeb(company.site_url)" :href="company.site_url" target="_blank" rel="noopener noreferrer">{{ host(company.site_url) }} ↗</a>
          <span v-else class="muted">No website on hh</span>
          · <a :href="company.url" target="_blank" rel="noopener noreferrer">hh profile ↗</a>
        </p>
        <p v-if="details" class="line muted details">{{ details }}</p>

        <CompanyIdentity :vacancy-id="vacancyId" :stored="company.identity"
                         :employer-name="company.name ?? companyName" />
        <RecruitersBlock :vacancy-id="vacancyId" :stored="company.recruiters" :search-name="searchName" />
      </template>
      <p v-else-if="!error" class="note">Its hh profile: website, city, industries. Free, about 2 s.</p>
    </template>
  </Card>
</template>

<style scoped>
.cardtop { display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; }
.line { margin: 0; font-size: 0.95rem; line-height: 1.6; }
.line a { color: var(--p-primary-color); text-decoration: none; }
.line a:hover { text-decoration: underline; }
.muted { color: var(--p-text-muted-color); }
.details { font-size: 0.85rem; }
.note { margin: 0; font-size: 0.78rem; line-height: 1.5; color: var(--p-text-muted-color); }
</style>
