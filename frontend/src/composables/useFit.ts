import type { Answers, FitLevel, Gap, Pitch } from '@/api/types'

/**
 * Every bit of vocabulary the UI needs, in one place.
 *
 * `best_angle` is the column name; "pitch" is the word a person understands.
 * The mapping lives here and nowhere else — nothing in the schema changes.
 */
export const PITCH_LABEL: Record<Pitch, string> = {
  backend: 'Backend engineering',
  ai_llm: 'AI / LLM',
  product: 'Product ownership',
  data: 'Data engineering',
  analytics: 'Analytics',
  ml: 'Machine learning',
  none: 'Other',
}

export const PITCH_HINT: Record<Pitch, string> = {
  backend: 'Python · Django · PostgreSQL depth',
  ai_llm: 'production LLM features, agentic tooling',
  product: 'end-to-end ownership, discovery to rollout',
  data: 'warehousing · ETL · columnar performance',
  analytics: 'SQL analysis · BI · product metrics · A/B tests',
  ml: 'training models — classical ML, DL, scoring, CV',
  none: 'no pitch fits this posting',
}

export const GAP_LABEL: Record<Gap, string> = {
  technical_skills: 'technical skills',
  domain_experience: 'domain experience',
  seniority_level: 'seniority level',
  none: 'none',
}

export const FIT_ORDER: FitLevel[] = ['poor', 'weak', 'good', 'strong', 'excellent']

/**
 * Fallback only. When `answers` is present the real descriptions come from its
 * `legend`, so a reworded question in questions.py reaches the UI with no
 * frontend change.
 */
const FALLBACK_LEGEND: Record<FitLevel, string> = {
  poor: 'Major mismatch in skills/experience.',
  weak: 'Some relevant experience but significant gaps.',
  good: 'Solid match with minor gaps.',
  strong: 'Very close match to requirements.',
  excellent: 'Ideal match, exceeds requirements.',
}

export function legendOf(answers: Answers | null): Record<string, string> {
  if (!answers) return FALLBACK_LEGEND
  const out: Record<string, string> = {}
  for (const entry of Object.values(answers.overall_fit.legend)) {
    out[entry.label] = entry.description
  }
  return out
}

/** `.78`, not `0.78` — the leading zero is noise in a dense column. */
export const fmtScore = (n: number | null | undefined) =>
  n === null || n === undefined ? '—' : n.toFixed(2).replace(/^0/, '')

export const fmtPct = (n: number) => `${Math.round(n * 100)}%`

/**
 * Below this the label is closer to a coin flip than a verdict, and the UI
 * fades it. .50 is a deliberate round number, not a tuned threshold — 14 of
 * 188 current matches sit under it.
 */
export const LOW_CONFIDENCE = 0.5
export const isVague = (confidence: number | null | undefined) =>
  confidence !== null && confidence !== undefined && confidence < LOW_CONFIDENCE

/** Probabilities of a Choice answer, sorted high to low, ready to render. */
export function distribution(
  probs: Record<string, number>,
  label: (key: string) => string,
): { key: string; label: string; p: number }[] {
  return Object.entries(probs)
    .map(([key, p]) => ({ key, label: label(key), p }))
    .sort((a, b) => b.p - a.p)
}
