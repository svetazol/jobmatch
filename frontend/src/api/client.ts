import type {
  Company, CompanyIdentity, CompanyRecruiters, CoverNote, Stats, TriageField, VacancyDetail, VacancyPage, VacancyQuery,
} from './types'
import { MOCK_STATS, mockCompany, mockDetail, mockIdentify, mockPage, mockRecruiters } from './mock'

/**
 * Opt-in, not opt-out: with no .env.local the app talks to the real API.
 * Set VITE_USE_MOCK=1 to develop against src/api/mock.ts with no backend.
 */
const USE_MOCK = import.meta.env.VITE_USE_MOCK === '1'
const BASE = '/api'

async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const url = new URL(BASE + path, window.location.origin)
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v === undefined || v === null || v === '' ) continue
    if (Array.isArray(v)) { for (const item of v) url.searchParams.append(k, String(item)) }
    else url.searchParams.set(k, String(v))
  }
  const res = await fetch(url, { headers: { accept: 'application/json' } })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url.pathname}`)
  return res.json() as Promise<T>
}

const delay = <T>(value: T, ms = 220) =>
  new Promise<T>((resolve) => setTimeout(() => resolve(value), ms))

export const api = {
  /**
   * `cv` is a CV file name from `stats.cvs`; omitted, the API answers for the
   * one pinned in config.toml. `country` narrows every aggregate but the
   * country breakdown itself, which stays whole-corpus so the filter keeps
   * its options.
   */
  stats(country: string[] = [], cv?: string): Promise<Stats> {
    return USE_MOCK ? delay(MOCK_STATS) : get<Stats>('/stats', { country, cv })
  },

  vacancies(q: VacancyQuery = {}): Promise<VacancyPage> {
    return USE_MOCK ? delay(mockPage()) : get<VacancyPage>('/vacancies', { ...q })
  },

  vacancy(id: number): Promise<VacancyDetail> {
    return USE_MOCK ? delay(mockDetail(id)) : get<VacancyDetail>(`/vacancies/${id}`)
  },

  /**
   * PATCH /api/vacancies/:id/triage — sets or clears one of seen_at /
   * starred_at / hidden_at / applied_at. `on: false` clears it, which is what Undo sends.
   */
  async triage(id: number, field: TriageField, on: boolean): Promise<void> {
    if (USE_MOCK) { await delay(null, 120); return }
    const res = await fetch(`${BASE}/vacancies/${id}/triage`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ [field]: on }),
    })
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  },
  /**
   * POST /api/vacancies/:id/cover-note — 10-30 s, since it runs `claude -p`.
   * A 502 carries claude's own error in `detail`, which is worth showing.
   */
  async coverNote(id: number): Promise<CoverNote> {
    if (USE_MOCK) {
      return delay({ text: 'Mock cover note: the backend would ask claude here.', language: 'English' }, 800)
    }
    const res = await fetch(`${BASE}/vacancies/${id}/cover-note`, { method: 'POST' })
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
    }
    return res.json() as Promise<CoverNote>
  },

  /** GET /api/vacancies/:id/company — what is stored; null until looked up. */
  company(id: number): Promise<Company | null> {
    return USE_MOCK ? delay(null) : get<Company | null>(`/vacancies/${id}/company`)
  },

  /**
   * POST /api/vacancies/:id/company — fetches the employer page, ~2 s. A 404
   * says why there is no employer to find (anonymous, removed); a 502 is hh
   * failing. Both carry the reason in `detail`.
   */
  async lookUpCompany(id: number): Promise<Company> {
    if (USE_MOCK) return delay(mockCompany(), 600)
    const res = await fetch(`${BASE}/vacancies/${id}/company`, { method: 'POST' })
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
    }
    return res.json() as Promise<Company>
  },

  /**
   * POST /api/vacancies/:id/company/identity — one model call, ~1-3 s and
   * ~$0.0002, up to ~90 s at worst (two 30 s tries, then two 10 s
   * reachability checks); free and instant when nothing it reads has changed. 409 until the company is looked up, 502 with the
   * model's error, 503 without OPENROUTER_API_KEY; each says so in `detail`.
   */
  async identifyCompany(id: number): Promise<CompanyIdentity> {
    if (USE_MOCK) return mockIdentify()
    const res = await fetch(`${BASE}/vacancies/${id}/company/identity`, { method: 'POST' })
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
    }
    return res.json() as Promise<CompanyIdentity>
  },

  /**
   * POST /api/vacancies/:id/company/recruiters — one AI web search, ~$0.01,
   * typically 3–5 s (measured 2026-10-14), up to ~60 s (30 s deadline, one
   * retry). Without `refresh`, free and instant when the same request was searched in the last 30 days;
   * with it, always a new paid search. 409 until the company is looked up,
   * 422 when it has neither a website nor a name to search for, 502 with the model's error, 503
   * without OPENROUTER_API_KEY; each says so in `detail`.
   */
  async findRecruiters(id: number, { refresh = false } = {}): Promise<CompanyRecruiters> {
    if (USE_MOCK) return mockRecruiters()
    const query = refresh ? '?refresh=true' : ''
    const res = await fetch(`${BASE}/vacancies/${id}/company/recruiters${query}`, { method: 'POST' })
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
    }
    return res.json() as Promise<CompanyRecruiters>
  },
}
