<script setup lang="ts">
import { computed } from 'vue'
import Card from 'primevue/card'
import type { Answers, Gap, MatchSummary, Pitch } from '@/api/types'
import FitHistogram from '@/components/FitHistogram.vue'
import {
  FIT_ORDER, GAP_LABEL, PITCH_HINT, PITCH_LABEL,
  fmtPct, fmtScore, isVague, legendOf, requirementsBand, topTwo,
} from '@/composables/useFit'

/**
 * The matcher's verdict as four sentences. An unsure answer names its two
 * leading options instead of one winner: a winner printed alone reads as
 * certainty the matcher did not have.
 */
const props = defineProps<{
  match: MatchSummary | null
  answers: Answers | null
}>()

const fitLabel = (key: string) => FIT_ORDER[Number(key)] ?? key
// "none" among two options must not read as a bare "or none" -- the "no gap" this card avoids
const gapLabel = (key: string) => key === 'none' ? 'no gap at all' : GAP_LABEL[key as Gap] ?? key
const pitchLabel = (key: string) => PITCH_LABEL[key as Pitch] ?? key

const fitVague = computed(() => isVague(props.match?.overall_fit_confidence))
const fitTop = computed(() => props.match ? topTwo(props.match.fit_probabilities, fitLabel) : [])
// The wording comes from questions.py via the stored legend.
const fitMeaning = computed(() =>
  props.match ? legendOf(props.answers)[props.match.overall_fit_label] : null)

const band = computed(() => props.match ? requirementsBand(props.match.is_qualified_noul) : null)

/**
 * Never render a vague gap as "no gap". Vacancy 40's top_gap is "none" at
 * confidence .17, with probabilities none .38 / seniority .26 / domain .25 /
 * technical .11. Printing the winner alone would read as "no gap" when what
 * the matcher actually said was "I can't tell".
 */
const gapVague = computed(() => isVague(props.match?.top_gap_confidence))
const gapTop = computed(() => props.answers ? topTwo(props.answers.top_gap.probabilities, gapLabel) : [])

const pitchVague = computed(() => isVague(props.match?.best_angle_confidence))
const pitchTop = computed(() => props.answers ? topTwo(props.answers.best_angle.probabilities, pitchLabel) : [])

const capitalise = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
</script>

<template>
  <Card>
    <template #content>
      <p v-if="!match" class="none">Not matched yet.</p>

      <div v-else class="rows">
        <span class="label">Fit</span>
        <div class="fit">
          <FitHistogram :probabilities="match.fit_probabilities" :label="match.overall_fit_label"
                        :confidence="match.overall_fit_confidence" />
          <p v-if="fitVague && fitTop.length === 2">
            <strong>{{ capitalise(match.overall_fit_label) }}?</strong> — the matcher was unsure:
            {{ fitTop[0]!.label }} {{ fmtPct(fitTop[0]!.p) }}, {{ fitTop[1]!.label }} {{ fmtPct(fitTop[1]!.p) }}.
          </p>
          <p v-else>
            <strong>{{ capitalise(match.overall_fit_label) }}</strong>
            <span class="nm">{{ fmtScore(match.overall_fit_score) }}</span>
            <span v-if="fitMeaning" class="muted">“{{ fitMeaning }}”</span>
          </p>
        </div>

        <span class="label">Requirements</span>
        <!-- borderline names the raw noul: .51 and .64 are both "true",
             and neither is worth trusting without a read -->
        <p v-if="band === 'meets'">Meets the minimum requirements.</p>
        <p v-else-if="band === 'borderline'">
          Borderline ({{ fmtScore(match.is_qualified_noul) }}) — read the requirements yourself before writing.
        </p>
        <p v-else>Misses the minimum requirements.</p>

        <span class="label">Biggest gap</span>
        <!-- the two shares, not "about equally": low confidence is not a tie
             (vacancy 5495: .63 against .19 at confidence .49) -->
        <p v-if="gapVague && gapTop.length === 2">
          No single clear gap — {{ gapTop[0]!.label }} {{ fmtPct(gapTop[0]!.p) }},
          {{ gapTop[1]!.label }} {{ fmtPct(gapTop[1]!.p) }}.
        </p>
        <p v-else-if="gapVague">No single clear gap.</p>
        <p v-else-if="match.top_gap === 'none'">No real gap.</p>
        <p v-else>{{ capitalise(GAP_LABEL[match.top_gap]) }}.</p>

        <span class="label">Lead with</span>
        <p v-if="pitchVague && pitchTop.length === 2">
          {{ pitchTop[0]!.label }} {{ fmtPct(pitchTop[0]!.p) }}, or {{ pitchTop[1]!.label }} {{ fmtPct(pitchTop[1]!.p) }}.
        </p>
        <p v-else>
          {{ PITCH_LABEL[match.best_angle] }} <span class="muted">— {{ PITCH_HINT[match.best_angle] }}</span>
        </p>
      </div>
    </template>
  </Card>
</template>

<style scoped>
.rows { display: grid; grid-template-columns: 8rem minmax(0, 1fr); gap: 0.875rem 1rem; align-items: baseline; }
.label { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--p-text-muted-color); }
.rows p, .none { margin: 0; font-size: 0.95rem; line-height: 1.5; }
.none { color: var(--p-text-muted-color); }
.fit { display: flex; align-items: flex-end; gap: 0.75rem; }
.nm { margin-left: 0.5rem; color: var(--p-text-muted-color); font-variant-numeric: tabular-nums; }
.muted { margin-left: 0.25rem; color: var(--p-text-muted-color); }
</style>
