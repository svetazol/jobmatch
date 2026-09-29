<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Card from 'primevue/card'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import ProgressBar from 'primevue/progressbar'
import Skeleton from 'primevue/skeleton'
import Message from 'primevue/message'
import { api } from '@/api/client'
import type { CvOption, Stats } from '@/api/types'
import FitDistributionChart from '@/components/FitDistributionChart.vue'
import PitchChart from '@/components/PitchChart.vue'
import { fmtScore } from '@/composables/useFit'

const stats = ref<Stats | null>(null)
const error = ref<string | null>(null)
const selected = ref<string[]>([])

/** Which CV the page reports on. Null until the first response says which one
 *  the API picked — config.toml owns that default, not the client. */
const cv = ref<string | null>(null)
const cvOptions = ref<CvOption[]>([])

/**
 * Held from the last response that asked for every country, so picking one
 * does not make the rest disappear from the list you picked it from. They are
 * refreshed rather than frozen because they are per-CV: two CVs can have
 * answered different parts of the corpus.
 */
const countryOptions = ref<{ name: string; n: number }[]>([])

/** Every percentage on this page divides by it, and a filter can legitimately
 *  select a country with no matches yet. */
const pct = (part: number, whole: number) => (whole ? Math.round((part / whole) * 100) : 0)

const filtered = computed(() => selected.value.length > 0)

async function load() {
  error.value = null
  try {
    const next = await api.stats(selected.value, cv.value ?? undefined)
    stats.value = next
    cv.value = next.cv?.name ?? null
    cvOptions.value = next.cvs
    if (!selected.value.length) countryOptions.value = next.countries
  }
  catch (e) { error.value = (e as Error).message }
}

/** Switching CV invalidates the country counts, and a country that one CV
 *  answered for may not exist under another. Start the new CV unfiltered. */
function switchCv() {
  selected.value = []
  load()
}

onMounted(load)
</script>

<template>
  <section class="page">
    <header class="head">
      <div>
        <h1>Market view</h1>
        <p v-if="stats" class="sub">
          {{ stats.total }} postings matched against
          <strong v-if="stats.cv">{{ stats.cv.name }}</strong><template v-else>your CV</template>
          <template v-if="filtered"> in {{ selected.join(', ') }}</template>
          <template v-else>
            ·
            <span v-for="(c, i) in stats.countries" :key="c.name">
              {{ i ? ' · ' : '' }}{{ c.name }} {{ c.n }}
            </span>
          </template>
        </p>
      </div>

      <MultiSelect v-model="selected" :options="countryOptions" option-label="name" option-value="name"
                   placeholder="All countries" display="chip" show-clear class="country"
                   aria-label="Filter by country" @change="load">
        <template #option="{ option }">
          <span class="opt">{{ option.name }}<span class="optn">{{ option.n }}</span></span>
        </template>
      </MultiSelect>
      <Select v-model="cv" :options="cvOptions" option-label="name" option-value="name"
              placeholder="Which CV" class="cv"
              aria-label="Which CV these numbers answer for" @change="switchCv">
        <template #option="{ option }">
          <span class="opt">
            {{ option.name }}<span v-if="!option.on_disk" class="gone" title="file no longer on disk">·</span>
            <span class="optn">{{ option.n }}</span>
          </span>
        </template>
      </Select>
    </header>

    <section v-if="stats" class="search" aria-label="How these postings were collected">
      <span class="searchlabel">Where these came from</span>
      <p class="searchline">
        Searched <strong>{{ stats.search.source }}</strong> for
        <strong class="kw">{{ stats.search.keyword }}</strong>
        <template v-if="stats.search.fields.length">
          in the {{ stats.search.fields.join(' and ') }}</template>.
      </p>
      <p v-if="stats.search.excluded.length" class="searchline">
        Postings mentioning
        <span v-for="(term, i) in stats.search.excluded" :key="term">
          <span class="ex">{{ term }}</span><template v-if="i < stats.search.excluded.length - 1">, </template>
        </span>
        were dropped before matching.
      </p>
    </section>

    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

    <div v-if="!stats && !error" class="kpis">
      <Skeleton v-for="n in 4" :key="n" height="8rem" border-radius="14px" />
    </div>

    <Message v-if="stats && !stats.total" severity="warn" :closable="false">
      No matched postings in {{ selected.join(', ') }} yet.
    </Message>

    <template v-if="stats && stats.total">
      <div class="kpis">
        <Card class="hero">
          <template #content>
            <div class="label">Worth applying to</div>
            <div class="heroline">
              <span class="big">{{ stats.worth_applying }}</span>
              <span class="of">of {{ stats.total }} matched</span>
            </div>
            <ProgressBar :value="pct(stats.worth_applying, stats.total)"
                         :show-value="false" style="height: 8px" />
            <p class="foot">Clears every must-have <em>and</em> scores 0.50 or better.</p>
          </template>
        </Card>

        <Card>
          <template #content>
            <div class="label">Qualified</div>
            <div class="med">{{ stats.qualified }}</div>
            <p class="foot">{{ pct(stats.qualified, stats.total) }}% meet every must-have</p>
          </template>
        </Card>

        <Card>
          <template #content>
            <div class="label">Median fit</div>
            <div class="med">{{ fmtScore(stats.median_fit) }}</div>
            <p class="foot">normalised 0–1 · mean {{ fmtScore(stats.mean_fit) }}</p>
          </template>
        </Card>

        <Card>
          <template #content>
            <div class="label">Spend</div>
            <div class="med">${{ stats.spend_usd.toFixed(3) }}</div>
            <p class="foot">{{ stats.calls }} calls · avg {{ stats.avg_duration_ms }} ms</p>
          </template>
        </Card>
      </div>

      <div class="charts">
        <Card>
          <template #title>Overall fit</template>
          <template #subtitle>The level Jev found most probable for each of the {{ stats.total }}</template>
          <template #content>
            <FitDistributionChart :distribution="stats.fit_distribution" />
            <p class="caption">
              <strong>{{ stats.fit_distribution.poor + stats.fit_distribution.weak }} of {{ stats.total }}</strong>
              are labelled weak or poor. Only
              <strong>{{ stats.fit_distribution.good + stats.fit_distribution.strong + stats.fit_distribution.excellent }}</strong>
              are labelled a solid match or better — a label, not the score the card
              above counts.
            </p>
          </template>
        </Card>

        <Card>
          <template #title>Fit by pitch</template>
          <template #subtitle>Which pitch the market is asking for — bar length is the number of postings</template>
          <template #content>
            <PitchChart :pitches="stats.pitches" />
            <p v-if="!filtered" class="caption">
              <strong>Backend engineering</strong> is the smallest lane and by far the strongest.
              <strong>Data engineering</strong> is more than twice its size and not one of its
              postings reaches a solid match.
            </p>
          </template>
        </Card>
      </div>
    </template>
  </section>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 0.875rem; }
.head { display: flex; align-items: flex-end; gap: 1.25rem; flex-wrap: wrap; margin-bottom: 0.25rem; }
.cv, .country { min-width: 14rem; }
.country { margin-left: auto; }
/* Select and MultiSelect size their own label off different theme tokens, so
   the CV value came out a size larger than "All countries" beside it. Both
   are pinned to the body size rather than one being nudged to match the
   other, which would only hold until the theme moved. */
.cv :deep(.p-select-label),
.country :deep(.p-multiselect-label) { font-size: 0.875rem; }
.gone { margin-left: 0.25rem; color: var(--p-text-muted-color); }
.opt { display: flex; gap: 0.75rem; align-items: baseline; width: 100%; }
.optn { margin-left: auto; font-size: 0.75rem; font-variant-numeric: tabular-nums;
  color: var(--p-text-muted-color); }
h1 { margin: 0; font-size: 1.375rem; font-weight: 600; letter-spacing: -0.02em; }
.sub { margin: 0.3rem 0 0; font-size: 0.8125rem; color: var(--p-text-muted-color); }
.search { padding: 0.9rem 1.1rem; background: var(--p-surface-0);
  border: 1px solid var(--p-content-border-color); border-left: 3px solid var(--p-primary-color);
  border-radius: 10px; }
.searchlabel { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--p-text-muted-color); }
.searchline { margin: 0.4rem 0 0; font-size: 0.8125rem; color: var(--p-text-color); }
.searchline strong { font-weight: 600; }
.kw { padding: 0.05rem 0.4rem; border-radius: 5px;
  background: var(--p-primary-color); color: var(--p-primary-contrast-color); }
.ex { padding: 0.05rem 0.4rem; border-radius: 5px; text-decoration: line-through;
  background: var(--p-surface-100); color: var(--p-text-muted-color); }
.kpis { display: grid; grid-template-columns: 1.6fr 1fr 1fr 1fr; gap: 0.875rem; }
.charts { display: grid; grid-template-columns: 420px minmax(0, 1fr); gap: 0.875rem; }
.label { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--p-text-muted-color); }
.heroline { display: flex; align-items: baseline; gap: 0.75rem; margin: 0.625rem 0 0.9rem; }
.big { font-size: 3.25rem; font-weight: 600; line-height: 1; letter-spacing: -0.035em; }
.med { font-size: 1.875rem; font-weight: 600; line-height: 1; letter-spacing: -0.025em; margin-top: 0.625rem; }
.of { font-size: 0.875rem; color: var(--p-text-muted-color); }
.foot { margin: 0.55rem 0 0; font-size: 0.78rem; color: var(--p-text-muted-color); }
.caption { margin: 1rem 0 0; font-size: 0.78rem; color: var(--p-text-muted-color); }
.caption strong { color: var(--p-text-color); }
@media (max-width: 1100px) {
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .charts { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 620px) { .kpis { grid-template-columns: minmax(0, 1fr); } }
</style>
