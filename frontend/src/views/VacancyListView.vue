<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Tag from 'primevue/tag'
import Button from 'primevue/button'
import MultiSelect from 'primevue/multiselect'
import SelectButton from 'primevue/selectbutton'
import ToggleButton from 'primevue/togglebutton'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'
import type { ToastMessageOptions } from 'primevue/toast'
import { api } from '@/api/client'
import type { Pitch, VacancyQuery, VacancyRow } from '@/api/types'
import FitHistogram from '@/components/FitHistogram.vue'
import { PITCH_LABEL, fmtScore, isVague } from '@/composables/useFit'

const router = useRouter()
const toast = useToast()

const rows = ref<VacancyRow[]>([])
const total = ref(0)
const cursor = ref<string | null>(null)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref<string | null>(null)

const q = ref('')
const unseenOnly = ref(false)
const unappliedOnly = ref(false)
const qualifiedOnly = ref(false)
const minFit = ref(0)
const pitches = ref<Pitch[]>([])
const countries = ref<string[]>([])

/**
 * Borrowed from the market view's breakdown rather than a route of its own.
 * Fetched once: that breakdown ignores the country filter, so the options
 * never shrink to what is already picked.
 */
const countryOptions = ref<string[]>([])
async function loadCountries() {
  try { countryOptions.value = (await api.stats()).countries.map((c) => c.name) }
  catch { /* the filter stays empty; the list itself still loads */ }
}

const pitchOptions = (Object.keys(PITCH_LABEL) as Pitch[])
  .map((p) => ({ label: PITCH_LABEL[p], value: p }))

// A threshold on the score, never on the label: the labels overlap
// (weak spans .13–.56, good .43–.62), so `label === 'strong'` is not a range.
const fitSteps = [
  { label: 'any', value: 0 },
  { label: '≥ good', value: 0.5 },
  { label: '≥ strong', value: 0.7 },
]

function filters(): VacancyQuery {
  return {
    q: q.value || undefined,
    unseen: unseenOnly.value || undefined,
    unapplied: unappliedOnly.value || undefined,
    qualified: qualifiedOnly.value || undefined,
    min_fit: minFit.value || undefined,
    pitch: pitches.value.length ? pitches.value : undefined,
    country: countries.value.length ? countries.value : undefined,
  }
}

/**
 * The filters the rows on screen were fetched with.
 *
 * `loadMore` has to send these rather than whatever the controls hold now:
 * the cursor is a keyset position in one ranking, and `q` is bound with
 * `v-model` while `load()` only fires on Enter. Typing in the search box and
 * clicking "Load more" without pressing Enter therefore asked the API to
 * continue the *unfiltered* ranking from inside the filtered one, and the
 * rows it returned were appended silently — a wrong answer with nothing on
 * screen to give it away.
 */
const applied = ref<VacancyQuery>(filters())

/** A filter change is a new query, so the cursor resets with it. */
async function load() {
  loading.value = true
  error.value = null
  const query = filters()
  try {
    const page = await api.vacancies(query)
    applied.value = query
    rows.value = page.items
    total.value = page.total
    cursor.value = page.next_cursor
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (!cursor.value || loadingMore.value) return
  loadingMore.value = true
  try {
    const page = await api.vacancies({ ...applied.value, cursor: cursor.value })
    rows.value = [...rows.value, ...page.items]
    cursor.value = page.next_cursor
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loadingMore.value = false
  }
}
onMounted(() => { load(); loadCountries() })

const visible = computed(() => rows.value.filter((r) => !r.hidden_at))

/** In a new tab, so the list keeps its filters, loaded pages and scroll. */
/** "3 Oct", with the year only when it is not this one; hover shows the full time. */
function fmtPosted(iso: string): string {
  const d = new Date(iso)
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(sameYear ? {} : { year: 'numeric' }) })
}

function open(row: VacancyRow) {
  window.open(router.resolve({ name: 'vacancy', params: { id: row.id } }).href, '_blank')
}

async function star(row: VacancyRow, event: Event) {
  event.stopPropagation()
  const on = !row.starred_at
  row.starred_at = on ? new Date().toISOString() : null
  await api.triage(row.id, 'starred', on)
}

async function toggleApplied(row: VacancyRow, event: Event) {
  event.stopPropagation()
  const on = !row.applied_at
  row.applied_at = on ? new Date().toISOString() : null
  await api.triage(row.id, 'applied', on)
}

async function hide(row: VacancyRow, event: Event) {
  event.stopPropagation()
  row.hidden_at = new Date().toISOString()
  await api.triage(row.id, 'hidden', true)
  // hidden_at is just a column, so undo is a real operation, not a trick
  // `row` rides along so the toast template in App.vue can put it back
  toast.add({
    severity: 'secondary',
    summary: `Hidden · ${row.company ?? 'vacancy'}`,
    life: 6000,
    group: 'undo',
    row,
  } as ToastMessageOptions & { row: VacancyRow })
}
</script>

<template>
  <section class="page">
    <header class="head">
      <div>
        <h1>Ranked by fit</h1>
        <p class="sub">{{ total }} matched · showing {{ visible.length }}</p>
      </div>
    </header>

    <!-- one filter row above everything it scopes -->
    <div class="filters">
      <InputText v-model="q" placeholder="Title, company, skill…" @keyup.enter="load" class="q" />
      <MultiSelect v-model="pitches" :options="pitchOptions" option-label="label" option-value="value"
                   placeholder="Any pitch" display="chip" @change="load" class="pitch" />
      <MultiSelect v-model="countries" :options="countryOptions" placeholder="Any country"
                   display="chip" show-clear aria-label="Filter by country" @change="load" class="country" />
      <SelectButton v-model="minFit" :options="fitSteps" option-label="label" option-value="value"
                    :allow-empty="false" @change="load" />
      <ToggleButton v-model="unseenOnly" on-label="Unseen only" off-label="Unseen only" @change="load" />
      <ToggleButton v-model="unappliedOnly" on-label="Not applied" off-label="Not applied" @change="load" />
      <ToggleButton v-model="qualifiedOnly" on-label="Qualified only" off-label="Qualified only" @change="load" />
    </div>

    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

    <DataTable :value="visible" :loading="loading" data-key="id" scrollable scroll-height="calc(100vh - 20rem)"
               :virtual-scroller-options="{ itemSize: 64 }" selection-mode="single"
               @row-click="open($event.data)" class="list">
      <Column field="match.overall_fit_score" header="Fit" :style="{ width: '8.5rem' }">
        <template #body="{ data }">
          <div class="fitcell">
            <FitHistogram :probabilities="data.match?.fit_probabilities ?? null"
                          :label="data.match?.overall_fit_label ?? null"
                          :confidence="data.match?.overall_fit_confidence ?? null" />
            <span class="score">{{ fmtScore(data.match?.overall_fit_score) }}</span>
          </div>
        </template>
      </Column>

      <Column header="Vacancy">
        <template #body="{ data }">
          <div class="title" :class="{ seen: data.seen_at }">
            {{ data.title }}
            <Tag v-if="data.applied_at" value="applied" severity="success" class="applied" />
          </div>
          <div class="meta">{{ data.company }} · {{ data.country }} · {{ data.work_formats.join(', ') }}</div>
        </template>
      </Column>

      <Column header="Posted" :style="{ width: '7rem' }">
        <template #body="{ data }">
          <span v-if="data.published_at" class="date" :title="new Date(data.published_at).toLocaleString()">
            {{ fmtPosted(data.published_at) }}
          </span>
          <span v-else class="muted">—</span>
        </template>
      </Column>

      <Column header="Pitch" :style="{ width: '13rem' }">
        <template #body="{ data }">
          <Tag v-if="data.match" :value="PITCH_LABEL[data.match.best_angle as Pitch]"
               severity="secondary"
               :class="{ vague: isVague(data.match.best_angle_confidence) }" />
          <span v-else class="muted">not matched yet</span>
        </template>
      </Column>

      <Column header="Requirements" :style="{ width: '9rem' }">
        <template #body="{ data }">
          <Tag v-if="data.match?.is_qualified" value="qualified" severity="contrast" />
          <Tag v-else-if="data.match" value="not qualified" severity="secondary" />
        </template>
      </Column>

      <Column :style="{ width: '8.5rem' }">
        <template #body="{ data }">
          <div class="acts">
            <Button :icon="data.starred_at ? 'pi pi-star-fill' : 'pi pi-star'" text rounded
                    :aria-label="data.starred_at ? 'Unstar' : 'Star'" @click="star(data, $event)" />
            <Button :icon="data.applied_at ? 'pi pi-check-circle' : 'pi pi-send'" text rounded
                    :severity="data.applied_at ? 'success' : undefined"
                    :title="data.applied_at ? 'Applied — click to undo' : 'Mark as applied'"
                    :aria-label="data.applied_at ? 'Mark as not applied' : 'Mark as applied'"
                    @click="toggleApplied(data, $event)" />
            <Button icon="pi pi-times" text rounded aria-label="Hide" @click="hide(data, $event)" />
          </div>
        </template>
      </Column>

      <template #footer>
        <div v-if="cursor" class="more">
          <Button label="Load more" :loading="loadingMore" severity="secondary"
                  outlined @click="loadMore" />
        </div>
      </template>

      <template #empty>
        <div class="empty">
          <p>Nothing matches these filters.</p>
          <p class="muted">Loosen one, or run the pipeline again.</p>
        </div>
      </template>
    </DataTable>
  </section>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 0.875rem; }
h1 { margin: 0; font-size: 1.375rem; font-weight: 600; letter-spacing: -0.02em; }
.sub { margin: 0.3rem 0 0; font-size: 0.8125rem; color: var(--p-text-muted-color); }
.filters { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.q { width: 16rem; }
.pitch, .country { min-width: 13rem; }
.fitcell { display: flex; align-items: center; gap: 0.625rem; }
.score { font-size: 0.95rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.title { font-size: 0.875rem; font-weight: 600; }
.title.seen { font-weight: 500; color: var(--p-text-muted-color); }
.meta { font-size: 0.75rem; color: var(--p-text-muted-color); margin-top: 0.15rem; }
.muted { font-size: 0.78rem; color: var(--p-text-muted-color); }
.date { font-size: 0.8125rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
.acts { display: flex; gap: 0.15rem; }
.applied { margin-left: 0.4rem; font-size: 0.68rem; padding: 0.05rem 0.4rem; vertical-align: 1px; }
.empty { padding: 3rem 1rem; text-align: center; }
.more { display: flex; justify-content: center; padding: 0.75rem 0; }
:deep(.vague) { opacity: 0.55; }
:deep(.p-datatable-tbody > tr) { cursor: pointer; }
</style>
