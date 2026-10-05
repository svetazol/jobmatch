/**
 * Mock payloads so the app runs with no backend.
 *
 * These are NOT invented numbers: every figure was read out of the project's
 * Postgres on 2026-09-24 (188 vacancies, 188 current matches, 376 llm_calls).
 * Keeping them real matters — the corpus is 89% noise, and a mock with a
 * flattering distribution would have hidden every layout problem that the
 * real shape creates.
 */
import type {
  Answers, FitLevel, Gap, Pitch, Stats, VacancyDetail, VacancyPage, VacancyRow,
} from './types'

const MOCK_CV = {
  name: 'cv2.md',
  cv_hash: '9e5bbcb28e19493e48b23ef128359014dc4fbbc38153d6261693d76225e1f4a5',
  n: 188,
  last_matched: '2026-09-24T21:11:26Z',
  on_disk: true,
}

export const MOCK_STATS: Stats = {
  cv: MOCK_CV,
  cvs: [
    MOCK_CV,
    { name: 'cv3.md', cv_hash: '7e959cf22260cda90795d1f2ca4169f35fed20f36efa1a924adb9473fe421de8',
      n: 188, last_matched: '2026-09-24T20:31:16Z', on_disk: true },
  ],
  total: 188,
  qualified: 29,
  worth_applying: 18,
  median_fit: 0.164,
  mean_fit: 0.199,
  spend_usd: 0.118034532,
  calls: 376,
  avg_duration_ms: 394,
  fit_distribution: { poor: 83, weak: 85, good: 6, strong: 9, excellent: 5 },
  // ordered by mean fit, not by size: the smallest lane is the best one.
  pitches: [
    { pitch: 'backend', n: 22, mean_fit: 0.421, qualified: 10, distribution: [0, 13, 3, 2, 4] },
    { pitch: 'ai_llm',  n: 43, mean_fit: 0.350, qualified: 11, distribution: [6, 26, 3, 7, 1] },
    { pitch: 'data',    n: 49, mean_fit: 0.172, qualified: 5,  distribution: [16, 33, 0, 0, 0] },
    { pitch: 'product', n: 3,  mean_fit: 0.155, qualified: 0,  distribution: [1, 2, 0, 0, 0] },
    { pitch: 'analytics', n: 0, mean_fit: 0.0,  qualified: 0,  distribution: [0, 0, 0, 0, 0] },
    { pitch: 'ml',      n: 0,  mean_fit: 0.0,   qualified: 0,  distribution: [0, 0, 0, 0, 0] },
    { pitch: 'none',    n: 71, mean_fit: 0.060, qualified: 3,  distribution: [60, 11, 0, 0, 0] },
  ],
  countries: [
    { name: 'Russia', n: 99 }, { name: 'Belarus', n: 65 }, { name: 'Georgia', n: 22 },
  ],
  work_formats: [
    { name: 'onsite', n: 119 }, { name: 'remote', n: 70 }, { name: 'hybrid', n: 50 },
  ],
  search: {
    source: 'hh.ru',
    keyword: 'python',
    fields: ['title', 'description'],
    excluded: ['QA', 'AQA', 'тестировщик', 'devops'],
  },
}

const row = (
  id: number, title: string, company: string, country: string, formats: string[],
  score: number, label: string, conf: number, qualified: boolean, noul: number,
  gap: string, gapConf: number, pitch: string, pitchConf: number,
  probs: [number, number, number, number, number],
): VacancyRow => ({
  id, title, company, country, work_formats: formats,
  url: `https://hh.ru/vacancy/${id}`,
  source: 'hh.ru',
  published_at: '2026-09-24T00:00:00Z',
  seen_at: null, starred_at: null, hidden_at: null, applied_at: null, delisted_at: null, stale: false,
  match: {
    overall_fit_score: score,
    overall_fit_label: label as FitLevel,
    overall_fit_confidence: conf,
    is_qualified: qualified,
    is_qualified_noul: noul,
    top_gap: gap as Gap,
    top_gap_confidence: gapConf,
    best_angle: pitch as Pitch,
    best_angle_confidence: pitchConf,
    fit_probabilities: { '0': probs[0], '1': probs[1], '2': probs[2], '3': probs[3], '4': probs[4] },
  },
})

export const MOCK_ROWS: VacancyRow[] = [
  row(90,  'Middle/Senior AI Agent Developer',        'Itransition',            'Belarus', ['remote'],                     0.85, 'strong',    0.71, true,  0.72, 'seniority_level',  0.31, 'ai_llm',  0.88, [0, 0.01, 0.07, 0.62, 0.30]),
  row(182, 'Python-разработчик',                      'ООО Вычислительные решения', 'Russia', ['onsite'],                  0.79, 'strong',    0.63, true,  0.66, 'seniority_level',  0.28, 'backend', 0.81, [0, 0.02, 0.14, 0.66, 0.18]),
  row(40,  'Lead AI Enablement Engineer / Tech Lead', 'ООО ОнТаргет ЛАБС',      'Georgia', ['remote'],                     0.78, 'strong',    0.79, true,  0.61, 'none',             0.17, 'ai_llm',  1.00, [0, 0, 0.06, 0.75, 0.19]),
  row(85,  'Python-разработчик',                      'ООО Финсельват',         'Belarus', ['remote', 'hybrid'],           0.77, 'excellent', 0.24, true,  0.58, 'seniority_level',  0.22, 'backend', 0.64, [0.01, 0.04, 0.24, 0.35, 0.36]),
  row(89,  'Junior Python Developer (Belarus)',       'GP Solutions',           'Belarus', ['remote', 'hybrid'],           0.76, 'excellent', 0.20, true,  0.55, 'seniority_level',  0.44, 'backend', 0.59, [0.01, 0.05, 0.26, 0.32, 0.36]),
  row(148, 'AI-инженер',                              'ООО Урал Логистика',     'Russia',  ['onsite', 'remote', 'hybrid'], 0.75, 'strong',    0.79, true,  0.69, 'technical_skills', 0.41, 'ai_llm',  0.83, [0, 0.02, 0.18, 0.60, 0.20]),
  row(135, 'Python-разработчик (Медиапортал)',        'Rambler&Co',             'Russia',  ['remote', 'hybrid'],           0.74, 'strong',    0.66, true,  0.63, 'seniority_level',  0.35, 'backend', 0.77, [0, 0.03, 0.20, 0.58, 0.19]),
  row(222, 'Backend-разработчик (middle)',            'ПАО «Газпром нефть» ИТ', 'Russia',  ['hybrid'],                     0.73, 'excellent', 0.08, true,  0.57, 'seniority_level',  0.29, 'backend', 0.52, [0.02, 0.06, 0.28, 0.30, 0.34]),
]

/** Vacancy 40's real `answers` blob, copied out of match_results verbatim. */
const ANSWERS_40: Answers = {
  overall_fit: {
    type: 'score',
    score: 3.13,
    confidence: 0.79,
    probabilities: { '0': 0.0, '1': 0.0, '2': 0.06, '3': 0.75, '4': 0.19 },
    legend: {
      '0': { label: 'poor', description: 'Major mismatch in skills/experience.' },
      '1': { label: 'weak', description: 'Some relevant experience but significant gaps.' },
      '2': { label: 'good', description: 'Solid match with minor gaps.' },
      '3': { label: 'strong', description: 'Very close match to requirements.' },
      '4': { label: 'excellent', description: 'Ideal match, exceeds requirements.' },
    },
  },
  is_qualified: { type: 'noul', noul: 0.61 },
  // choice "none" at .17 confidence — a four-way tie. The UI must not print
  // this as "no gap"; see VacancyDetailView.
  top_gap: {
    type: 'choice',
    choice: 'none',
    confidence: 0.17,
    probabilities: { none: 0.38, seniority_level: 0.26, domain_experience: 0.25, technical_skills: 0.11 },
  },
  best_angle: {
    type: 'choice',
    choice: 'ai_llm',
    confidence: 1.0,
    probabilities: { ai_llm: 1.0, backend: 0.0, product: 0.0, data: 0.0, none: 0.0 },
  },
}

const DESC_40 = `OnTarget Labs is a leading international software product development company.
We create next generation of world class product lines.
The company is looking for a Lead AI Enablement Engineer, AI-First DevEx to join our innovative product team as a full-time member working REMOTELY.
Lots of opportunities for professional growth and business trips abroad are offered.
Join our friendly team of IT professionals now!

Product description

A fast-growing B2B SaaS platform in the US property management industry. Our platform supports complex multi-tenant workflows, integrations, payments, and operational processes that real businesses rely on every day. As we grow, we are investing in AI-first engineering, developer experience, and modern engineering foundations so product teams can ship faster, safer, and with more confidence. We are looking for a hands-on AI enablement engineer to make AI-assisted delivery practical, repeatable, trusted, and tied to real engineering outcomes.

The Role

This is a hands-on IC role inside the Platform team, with ownership for leading the AI enablement initiative across product engineering teams. You will partner closely with the Platform Lead to scale and improve AI-assisted workflows across the software delivery lifecycle.`

export function mockDetail(id: number): VacancyDetail {
  const base = MOCK_ROWS.find((r) => r.id === id) ?? MOCK_ROWS[0]!
  const is40 = base.id === 40
  return {
    ...base,
    // 124 of 188 postings state no salary — missing is the normal case.
    salary_raw: null,
    experience_raw: is40 ? 'более 6 лет' : 'от 3 до 6 лет',
    description: DESC_40,
    skills: is40
      ? ['AI enablement', 'AI coding agents', 'Technical lead', 'Английский — B2 — Средне-продвинутый']
      : ['Python', 'PostgreSQL', 'SQL', 'Linux'],
    fetched_at: '2026-09-24T09:12:00Z',
    answers: is40 ? ANSWERS_40 : ANSWERS_40,
    cost_usd: '0.000314',
  }
}

export function mockPage(): VacancyPage {
  return { items: MOCK_ROWS, total: 188, next_cursor: null }
}
