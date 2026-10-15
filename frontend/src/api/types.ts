/**
 * The contract between this app and `jobmatch/api/`. Field names match the
 * SQLAlchemy columns in `jobmatch/models.py` so there is nothing to translate
 * on either side — the one place a name changes is `best_angle`, which the UI
 * shows as "pitch" (see composables/useFit.ts) and nowhere else.
 */

export type FitLevel = 'poor' | 'weak' | 'good' | 'strong' | 'excellent'
export type Pitch = 'backend' | 'ai_llm' | 'product' | 'data' | 'analytics' | 'ml' | 'none'
export type Gap = 'technical_skills' | 'domain_experience' | 'seniority_level' | 'none'

/** One row of the ranked list. Everything the list can sort or filter by. */
export interface VacancyRow {
  id: number
  title: string
  company: string | null
  country: string | null
  work_formats: string[]
  url: string
  source: string
  published_at: string | null
  seen_at: string | null
  starred_at: string | null
  hidden_at: string | null
  applied_at: string | null
  delisted_at: string | null
  stale: boolean
  /** null when the vacancy is fetched but not matched yet — a real state, not a zero. */
  match: MatchSummary | null
}

export interface MatchSummary {
  overall_fit_score: number      // 0-1, THE sort key
  overall_fit_label: FitLevel    // most probable level, NOT derivable from the score
  overall_fit_confidence: number
  is_qualified: boolean
  is_qualified_noul: number      // the raw value; .51 and .99 are both "true"
  top_gap: Gap
  top_gap_confidence: number
  best_angle: Pitch
  best_angle_confidence: number
  /**
   * The five stored fit probabilities, keyed "0".."4" like the legend.
   * Projected out of `answers` by the list endpoint: five floats per row is
   * cheap, and without them the list can only show a number, which is the
   * one thing the design is trying not to do.
   */
  fit_probabilities: Record<string, number>
}

/** GET /api/vacancies/:id — the row plus everything only the detail view needs. */
export interface VacancyDetail extends VacancyRow {
  salary_raw: string | null
  experience_raw: string | null
  description: string | null
  skills: string[]
  fetched_at: string | null
  /** When discovery last saw it in search; `stale` is measured from it. */
  last_seen_at: string
  answers: Answers | null
  /** hh's experience bucket against the configured years; "below" asks for less than the user has. */
  seniority: Seniority | null
}

export type Seniority = 'below' | 'level' | 'above'

/** The `answers` JSONB, verbatim. The legend travels with it, so the UI never
 *  hardcodes what "weak" means — questions.py stays the single source. */
export interface Answers {
  overall_fit: {
    type: 'score'
    score: number                               // raw, on the legend's index scale
    confidence: number
    probabilities: Record<string, number>       // "0".."4"
    legend: Record<string, { label: string; description: string }>
  }
  is_qualified: { type: 'noul'; noul: number }
  top_gap: { type: 'choice'; choice: Gap; confidence: number; probabilities: Record<string, number> }
  best_angle: { type: 'choice'; choice: Pitch; confidence: number; probabilities: Record<string, number> }
}

/** POST /api/vacancies/:id/cover-note — drafted by `claude -p`, never stored. */
export interface CoverNote {
  text: string
  language: 'English' | 'Russian'
}

/**
 * GET/POST /api/vacancies/:id/company — the employer behind a vacancy, as its
 * page said when last looked up. `url` is the employer's page on the source;
 * `site_url` is whatever the employer links from it, often a careers site.
 */
export interface Company {
  name: string | null
  url: string
  site_url: string | null
  /** Verbatim from the hh page, in its language; null when the page omits it. */
  city: string | null
  category: string | null
  industries: string | null
  looked_up_at: string
  /** The newest answer for this posting; null until identified. */
  identity: CompanyIdentity | null
  /** The newest search for this company's request; null until searched. */
  recruiters: CompanyRecruiters | null
}

/**
 * POST /api/vacancies/:id/company/identity — who is behind the posting, as a
 * model answered it. Every null means "unknown": the model is told to say
 * so rather than guess. A URL is worth a link only when its `_ok` is true.
 */
export interface CompanyIdentity {
  employer: string | null
  homepage: string | null
  careers_page: string | null
  is_agency: boolean | null
  /** The agency's client, named only when the posting names it. */
  client: string | null
  notes: string
  /** Did the URL answer a GET; null when there was no URL to try. */
  homepage_ok: boolean | null
  careers_ok: boolean | null
  model: string
  cost_usd: number | null
  /** Read in full with `jobmatch trace <id>`. */
  llm_call_id: number
  created_at: string
}

/**
 * POST /api/vacancies/:id/company/recruiters — the people an AI web search
 * named, kept only when the search itself cited their LinkedIn profile.
 * A citation proves the profile exists, not that the name and role match it,
 * so `evidence` and `source_title` travel with each person for the user to read.
 */
export interface CompanyRecruiters {
  people: RecruiterPerson[]
  /** Named by the model but not among the links the search returned. */
  dropped: number
  /** Links the search returned; 0 means there was nothing to check against. */
  sources: number
  created_at: string
}

export interface RecruiterPerson {
  name: string
  role: string | null
  /** Canonical `https://www.linkedin.com/in/<slug>`, rewritten from the cited link. */
  linkedin_url: string
  /** The model's quote of the search result it read the person from. */
  evidence: string
  /** The cited page's title, as the search returned it. */
  source_title: string | null
  /** When the user marked that they wrote to this person; null when unmarked. */
  written_at: string | null
}

export interface VacancyPage {
  items: VacancyRow[]
  total: number
  /** keyset on (overall_fit_score, vacancy_id) — matches ix_match_results_current_rank */
  next_cursor: string | null
}

export interface VacancyQuery {
  pitch?: Pitch[]
  min_fit?: number
  qualified?: boolean
  unseen?: boolean
  unapplied?: boolean
  posted_days?: number
  country?: string[]
  work_format?: string[]
  q?: string
  /** 'fit' (the default) or 'posted', newest first. A cursor only continues the sort it came from. */
  sort?: 'fit' | 'posted'
  cursor?: string | null
  limit?: number
}

/** One CV the corpus holds answers from. `name` is the file when it is still
 *  on disk and a truncated hash when it is not — and it is also what `?cv=`
 *  takes, so the picker round-trips. */
export interface CvOption {
  name: string
  cv_hash: string
  n: number               // vacancies this CV has an answer for
  last_matched: string | null
  on_disk: boolean
}

/** GET /api/stats */
export interface Stats {
  /** which CV every figure below answers for, and which others could be asked */
  cv: CvOption | null
  cvs: CvOption[]
  total: number
  qualified: number
  worth_applying: number          // qualified AND fit >= 0.5
  median_fit: number
  mean_fit: number
  spend_usd: number
  calls: number
  avg_duration_ms: number
  fit_distribution: Record<FitLevel, number>
  pitches: PitchStat[]
  countries: { name: string; n: number }[]
  work_formats: { name: string; n: number }[]
  search: Search
}

/** What produced the corpus — shown, not buried in config.toml. */
export interface Search {
  source: string
  keyword: string
  fields: string[]
  excluded: string[]
}

export interface PitchStat {
  pitch: Pitch
  n: number
  mean_fit: number
  qualified: number
  /** counts per fit level, in ladder order poor -> excellent */
  distribution: [number, number, number, number, number]
}

export type TriageField = 'seen' | 'starred' | 'hidden' | 'applied'
