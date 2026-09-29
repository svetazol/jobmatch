<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Card from 'primevue/card'
import Tag from 'primevue/tag'
import Chip from 'primevue/chip'
import Button from 'primevue/button'
import Panel from 'primevue/panel'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import { api } from '@/api/client'
import type { Gap, Pitch, VacancyDetail } from '@/api/types'
import ChoiceDistribution from '@/components/ChoiceDistribution.vue'
import FitHistogram from '@/components/FitHistogram.vue'
import { GAP_LABEL, PITCH_LABEL, fmtScore, isVague, legendOf } from '@/composables/useFit'

const route = useRoute()
const router = useRouter()
const v = ref<VacancyDetail | null>(null)
const error = ref<string | null>(null)

async function load(id: number) {
  error.value = null
  v.value = null
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
function markSeen(row: VacancyDetail) {
  if (row.seen_at) return
  row.seen_at = new Date().toISOString()
  api.triage(row.id, 'seen', true).catch(() => { /* a read, not a promise */ })
}
onMounted(() => load(Number(route.params.id)))
watch(() => route.params.id, (id) => load(Number(id)))

const a = computed(() => v.value?.answers ?? null)
const legend = computed(() => legendOf(a.value))

/** The winning level's own description, straight from the stored legend. */
const fitMeaning = computed(() => {
  const label = v.value?.match?.overall_fit_label
  return label ? legend.value[label] : null
})

/**
 * The single most important rule on this screen. `top_gap` is a stored winner,
 * but when confidence is low the winner is meaningless — vacancy 40 has
 * choice "none" at .17 with a near four-way tie behind it. Rendering that as
 * "no gap" would invert what the matcher actually said.
 */
const gapIsVague = computed(() => isVague(v.value?.match?.top_gap_confidence))

const pitchLabel = (key: string) => PITCH_LABEL[key as Pitch] ?? key
const gapLabel = (key: string) => GAP_LABEL[key as Gap] ?? key
</script>

<template>
  <section class="page">
    <nav class="crumb">
      <Button icon="pi pi-chevron-left" label="All matches" text @click="router.push({ name: 'list' })" />
    </nav>

    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>
    <Skeleton v-if="!v && !error" height="20rem" border-radius="14px" />

    <template v-if="v">
      <Card>
        <template #content>
          <div class="topline">
            <div class="titlebox">
              <h1>{{ v.title }}</h1>
              <p class="org">{{ v.company }} · {{ v.country }} · {{ v.work_formats.join(', ') }}</p>
            </div>
            <a :href="v.url" target="_blank" rel="noopener">
              <Button label="Open on hh.ru" icon="pi pi-external-link" icon-pos="right" />
            </a>
          </div>
          <div class="facts">
            <Tag v-if="v.experience_raw" :value="v.experience_raw" severity="secondary" />
            <!-- 124 of 188 postings state no salary, so absent is the normal case -->
            <Tag v-if="v.salary_raw" :value="v.salary_raw" severity="secondary" />
            <span v-else class="nosalary">salary not stated</span>
          </div>
        </template>
      </Card>

      <div class="verdict">
        <Card>
          <template #content>
            <div class="cardtop">
              <span class="label">Overall fit</span>
              <span class="conf">confidence {{ fmtScore(v.match?.overall_fit_confidence) }}</span>
            </div>
            <div class="headline">
              <span class="lv">{{ v.match?.overall_fit_label ?? 'not matched yet' }}</span>
              <span class="nm">{{ fmtScore(v.match?.overall_fit_score) }}</span>
            </div>
            <FitHistogram size="panel"
                          :probabilities="a?.overall_fit.probabilities ?? null"
                          :label="v.match?.overall_fit_label ?? null"
                          :confidence="v.match?.overall_fit_confidence ?? null" />
            <!-- the wording comes from questions.py via the stored legend -->
            <p v-if="fitMeaning" class="note">“{{ fitMeaning }}”</p>
          </template>
        </Card>

        <Card>
          <template #content>
            <div class="cardtop">
              <span class="label">Lead with</span>
              <span class="conf">confidence {{ fmtScore(v.match?.best_angle_confidence) }}</span>
            </div>
            <div class="headline">
              <span class="lv">{{ v.match ? PITCH_LABEL[v.match.best_angle] : '—' }}</span>
            </div>
            <ChoiceDistribution v-if="a" :probabilities="a.best_angle.probabilities" :label-for="pitchLabel" />
          </template>
        </Card>

        <Card>
          <template #content>
            <span class="label">Minimum requirements</span>
            <div class="headline">
              <span class="lv">{{ v.match?.is_qualified ? 'Met' : 'Not met' }}</span>
              <span class="nm">{{ fmtScore(v.match?.is_qualified_noul) }}</span>
            </div>
            <!-- the raw noul against its .50 threshold: .61 and .99 are both
                 "true", and only one of them is worth trusting -->
            <div class="qbar">
              <span class="fill" :style="{ width: ((v.match?.is_qualified_noul ?? 0) * 100) + '%' }"></span>
              <span class="thresh" aria-hidden="true"></span>
            </div>
            <div class="qscale"><span>not met</span><span>threshold .50</span><span>clearly met</span></div>
            <p v-if="v.match && Math.abs(v.match.is_qualified_noul - 0.5) < 0.15" class="note">
              Close to the line — read the requirements yourself before writing anything.
            </p>
          </template>
        </Card>

        <Card>
          <template #content>
            <div class="cardtop">
              <span class="label">Biggest gap</span>
              <span class="conf">confidence {{ fmtScore(v.match?.top_gap_confidence) }}</span>
            </div>
            <div class="headline">
              <span class="lv" :class="{ soft: gapIsVague }">
                {{ gapIsVague ? 'No clear single gap' : GAP_LABEL[v.match?.top_gap ?? 'none'] }}
              </span>
            </div>
            <ChoiceDistribution v-if="a" :probabilities="a.top_gap.probabilities" :label-for="gapLabel" />
            <p v-if="gapIsVague" class="note">
              The matcher could not pick one. Do not read this as “no gap” — treat the
              top few as open questions.
            </p>
          </template>
        </Card>
      </div>

      <Card>
        <template #title>Key skills</template>
        <template #content>
          <div class="skills">
            <Chip v-for="s in v.skills" :key="s" :label="s" />
          </div>
          <p v-if="!v.skills.length" class="note">
            No skill tags — 66 of 188 postings have none, so the match ran on description text alone.
          </p>
        </template>
      </Card>

      <Panel header="Description" toggleable :collapsed="true">
        <!-- plain text with list bullets already lost in scraping -->
        <pre class="desc">{{ v.description }}</pre>
      </Panel>

      <p class="prov">
        {{ v.source }} · fetched {{ v.fetched_at?.slice(0, 10) }} · matched with jev-1.13 · ${{ v.cost_usd }}
      </p>
    </template>
  </section>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 0.875rem; }
.crumb { margin-bottom: -0.25rem; }
.topline { display: flex; gap: 1.25rem; align-items: flex-start; flex-wrap: wrap; }
.titlebox { flex: 1 1 20rem; min-width: 0; }
h1 { margin: 0; font-size: 1.5rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.25; }
.org { margin: 0.5rem 0 0; font-size: 0.875rem; color: var(--p-text-muted-color); }
.facts { margin-top: 1rem; display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.nosalary { font-size: 0.78rem; font-style: italic; color: var(--p-text-muted-color); }
.verdict { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.875rem; }
.cardtop { display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; }
.label { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--p-text-muted-color); }
.conf { font-size: 0.72rem; color: var(--p-text-muted-color); font-variant-numeric: tabular-nums; }
.headline { display: flex; align-items: baseline; gap: 0.625rem; margin-top: 0.625rem; }
.lv { font-size: 1.625rem; font-weight: 600; letter-spacing: -0.02em; }
.lv.soft { font-size: 1.25rem; color: var(--p-text-muted-color); }
.nm { font-size: 0.95rem; color: var(--p-text-muted-color); font-variant-numeric: tabular-nums; }
.note { margin: 0.875rem 0 0; font-size: 0.78rem; line-height: 1.5; color: var(--p-text-muted-color); }
.qbar { position: relative; height: 12px; margin-top: 0.875rem; background: var(--p-surface-200);
  border-radius: 6px; overflow: visible; }
.qbar .fill { display: block; height: 100%; background: var(--p-primary-color); border-radius: 6px; }
.qbar .thresh { position: absolute; top: -4px; bottom: -4px; left: 50%; width: 2px;
  background: var(--p-text-color); opacity: 0.55; }
.qscale { margin-top: 0.4rem; display: flex; justify-content: space-between;
  font-size: 0.7rem; color: var(--p-text-muted-color); }
.skills { display: flex; flex-wrap: wrap; gap: 0.45rem; }
.desc { margin: 0; font-family: inherit; font-size: 0.85rem; line-height: 1.65; white-space: pre-wrap; }
.prov { margin: 0; font-size: 0.72rem; color: var(--p-text-muted-color); font-variant-numeric: tabular-nums; }
@media (max-width: 900px) { .verdict { grid-template-columns: minmax(0, 1fr); } }
</style>
