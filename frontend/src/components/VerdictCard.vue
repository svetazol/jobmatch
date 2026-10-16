<script setup lang="ts">
import { computed } from 'vue'
import type { Answers, Gap, MatchSummary, Pitch, Seniority } from '@/api/types'
import {
  FIT_ORDER, GAP_LABEL, PITCH_HINT, PITCH_LABEL,
  fmtPct, fmtScore, isVague, legendOf, requirementsBand, topTwo,
} from '@/composables/useFit'

/**
 * The matcher's verdict: the fit as a ring, then four short columns. An unsure
 * answer names its two leading options instead of one winner: a winner printed
 * alone reads as certainty the matcher did not have.
 */
const props = defineProps<{
  match: MatchSummary | null
  answers: Answers | null
  seniority: Seniority | null
}>()

const fitLabel = (key: string) => FIT_ORDER[Number(key)] ?? key
// "none" among two options must not read as a bare "or none" -- the "no gap" this card avoids
const gapLabel = (key: string) => key === 'none' ? 'no gap at all' : GAP_LABEL[key as Gap] ?? key
const pitchLabel = (key: string) => PITCH_LABEL[key as Pitch] ?? key

const capitalise = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const share = (t: { label: string; p: number }) => `${t.label} ${fmtPct(t.p)}`

const RING_R = 34
const RING_LEN = 2 * Math.PI * RING_R
const ringDash = computed(() => `${(props.match?.overall_fit_score ?? 0) * RING_LEN} ${RING_LEN}`)
// bright on purpose (the user's call, 2026-10-15): green only for strong and up, so a poor fit never reads as a pass
const RING_COLOUR: Record<string, string> = {
  excellent: 'var(--p-green-500)', strong: 'var(--p-green-500)', good: 'var(--p-primary-color)',
  weak: 'var(--p-orange-500)', poor: 'var(--p-red-500)',
}
const ringColour = computed(() => props.match ? RING_COLOUR[props.match.overall_fit_label] ?? 'var(--p-primary-color)' : undefined)

const fitVague = computed(() => isVague(props.match?.overall_fit_confidence))
const fitTop = computed(() => props.match ? topTwo(props.match.fit_probabilities, fitLabel) : [])
// The wording comes from questions.py via the stored legend.
const fitMeaning = computed(() =>
  props.match ? legendOf(props.answers)[props.match.overall_fit_label] : null)

const band = computed(() => props.match ? requirementsBand(props.match.is_qualified_noul) : null)

const SENIORITY_TEXT: Record<Seniority, string> = {
  below: 'Below your level', level: 'Your level', above: 'Above your level',
}

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
</script>

<template>
  <p v-if="!match" class="none">Not matched yet.</p>

  <div v-else class="verdict">
    <div class="fit">
      <svg class="ring" viewBox="0 0 80 80" width="80" height="80" role="img"
           :aria-label="`Fit score ${Math.round(match.overall_fit_score * 100)} of 100`">
        <circle cx="40" cy="40" :r="RING_R" class="track" />
        <circle cx="40" cy="40" :r="RING_R" class="arc" :stroke="ringColour" :stroke-dasharray="ringDash" />
        <text x="40" y="40" class="score">{{ Math.round(match.overall_fit_score * 100) }}</text>
      </svg>
      <div v-if="fitVague && fitTop.length === 2">
        <p class="fitlabel">{{ capitalise(match.overall_fit_label) }}?</p>
        <p class="muted">unsure: {{ share(fitTop[0]!) }} or {{ share(fitTop[1]!) }}</p>
      </div>
      <div v-else>
        <p class="fitlabel">{{ capitalise(match.overall_fit_label) }}</p>
        <p v-if="fitMeaning" class="muted">{{ fitMeaning }}</p>
      </div>
    </div>

    <div class="cols">
      <div>
        <span class="label">Requirements</span>
        <!-- borderline names the raw noul: .51 and .64 are both "true",
             and neither is worth trusting without a read -->
        <p v-if="band === 'meets'" class="value ok">Meets</p>
        <p v-else-if="band === 'borderline'" class="value warn">Borderline · {{ fmtScore(match.is_qualified_noul) }}</p>
        <p v-else class="value bad">Misses</p>
        <p class="muted">{{ band === 'borderline' ? 'read them before writing' : 'minimum requirements' }}</p>
      </div>

      <div>
        <span class="label">Seniority</span>
        <template v-if="seniority">
          <p class="value">{{ SENIORITY_TEXT[seniority] }}</p>
          <span class="scale" aria-hidden="true"><span class="you" /><span :class="['dot', seniority]" /></span>
        </template>
        <template v-else>
          <p class="value">—</p>
          <p class="muted">experience not stated</p>
        </template>
      </div>

      <div>
        <span class="label">Biggest gap</span>
        <!-- the two shares, not "about equally": low confidence is not a tie
             (vacancy 5495: .63 against .19 at confidence .49) -->
        <template v-if="gapVague">
          <p class="value">Unclear</p>
          <p class="muted">no single gap stands out</p>
          <p v-if="gapTop.length === 2" class="muted">{{ share(gapTop[0]!) }} · {{ share(gapTop[1]!) }}</p>
        </template>
        <p v-else-if="match.top_gap === 'none'" class="value">No real gap</p>
        <p v-else class="value">{{ capitalise(GAP_LABEL[match.top_gap]) }}</p>
      </div>

      <div>
        <span class="label">Lead with</span>
        <template v-if="pitchVague && pitchTop.length === 2">
          <p class="value">{{ pitchTop[0]!.label }}?</p>
          <p class="muted">{{ fmtPct(pitchTop[0]!.p) }}, or {{ share(pitchTop[1]!) }}</p>
        </template>
        <template v-else>
          <p class="value">{{ PITCH_LABEL[match.best_angle] }}</p>
          <p class="muted">{{ PITCH_HINT[match.best_angle] }}</p>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
p { margin: 0; }
.none { font-size: 0.95rem; color: var(--p-text-muted-color); }
.muted { font-size: 0.8rem; line-height: 1.45; color: var(--p-text-muted-color); }

.fit { display: flex; align-items: center; gap: 1rem; }
.ring { flex: none; }
.ring circle { fill: none; stroke-width: 7; }
.track { stroke: var(--p-surface-200); }
/* starts at twelve o'clock and fills clockwise */
.arc { stroke-linecap: round; transform: rotate(-90deg); transform-origin: 40px 40px; }
.score { fill: var(--p-text-color); font-size: 26px; font-weight: 700; text-anchor: middle;
  dominant-baseline: central; }
.fitlabel { font-size: 1.15rem; font-weight: 600; }

.cols { margin-top: 1.25rem; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1rem 1.5rem; }
/* the global small-caps label, set on its own line above each column's value */
.label { display: block; margin-bottom: 0.3rem; }
.value { font-size: 0.95rem; font-weight: 600; line-height: 1.4; }
.ok { color: var(--status-ok); }
.warn { color: var(--status-warn); }
.bad { color: var(--status-bad); }

/* the green band is your level, the dot the posting: green on your level, orange off it */
.scale { position: relative; display: block; width: 120px; height: 4px; margin-top: 0.55rem;
  border-radius: 2px; background: var(--p-surface-200); }
.you { position: absolute; left: 40%; width: 20%; height: 100%; border-radius: 2px; background: var(--p-green-500); }
.dot { position: absolute; top: 50%; width: 10px; height: 10px; border-radius: 50%;
  transform: translate(-50%, -50%); background: var(--p-orange-500); }
.dot.below { left: 0; }
.dot.level { left: 50%; background: var(--p-green-600); }
.dot.above { left: 100%; }

@media (max-width: 860px) { .cols { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 480px) { .cols { grid-template-columns: minmax(0, 1fr); } }
</style>
