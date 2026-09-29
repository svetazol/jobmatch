# jobmatch frontend

Vue 3 + Vite + TypeScript + PrimeVue. Reads the same database as the pipeline,
through `jobmatch/api/` — it never talks to Postgres itself.

## Running it

Two terminals. The API first, from the repo root:

```bash
uv run uvicorn jobmatch.api.app:app --reload   # http://127.0.0.1:8000
```

then the app:

```bash
cd frontend
npm install
npm run dev                                    # http://localhost:5173
```

Vite proxies `/api` to `127.0.0.1:8000`, so there is nothing to configure —
the app reads the real database through `jobmatch/api/`.

**No backend handy?** Set `VITE_USE_MOCK=1` in `.env.local` and every request
is served from `src/api/mock.ts` instead. Those payloads are **real figures
read out of the project's Postgres on 2026-09-24** (188 vacancies, 188 current
matches, 376 `llm_calls`, $0.118 spent), not invented sample data — the corpus
is 89% noise, and a flattering mock would hide every layout problem the real
shape creates.

## The API this expects

`src/api/types.ts` is the contract. Field names match the columns in
`jobmatch/models.py`, so there is nothing to translate on either side.

| Route | Returns |
|---|---|
| `GET /api/stats?since=30d` | `Stats` — headline figures, fit distribution, per-pitch breakdown |
| `GET /api/vacancies?…` | `VacancyPage` — ranked slice, keyset cursor on `(overall_fit_score, id)` |
| `GET /api/vacancies/:id` | `VacancyDetail` — the row plus `description`, `skills` and the whole `answers` JSONB |
| `PATCH /api/vacancies/:id/triage` | `{seen\|starred\|hidden: boolean}` — sets or clears the timestamp |

The list endpoint projects `fit_probabilities` (the five stored floats) onto
every row — without them the list can only print a number, and the number alone
is the thing this design avoids — and orders by `overall_fit_score DESC, id` so
the partial index `ix_match_results_current_rank` does the work. `next_cursor`
is `"<score>:<id>"`; the list sends it back to append the next page.

## Three decisions that are not cosmetic

**Colour encodes fit and nothing else.** One hue, light to dark, five steps.
The ramps in `src/theme.ts` were run through an OKLab/Machado colourblind and
WCAG validator and pass as ordinal ramps in both light and dark mode. An
earlier attempt at four categorical hues for the pitches failed badly — two
below the chroma floor, worst pair ΔE 3.4 under deuteranopia. Do not hand-edit
a step without re-validating the set.

**Pitch identity is carried by its label, never by hue.** `best_angle` is the
column name; "pitch" is the word a person understands, and the mapping lives in
`src/composables/useFit.ts` alone.

**A low-confidence answer is never printed as a fact.** Vacancy 40's `top_gap`
is `none` at confidence `.17` with a near four-way tie behind it; shown as
"Gap: none" that inverts what the matcher said. Below `.50` the UI shows the
whole distribution and says it could not pick one. Same rule fades the pitch
tag and the fit histogram.

## Notes

- Level names (`poor`…`excellent`) and their descriptions come from the stored
  `answers.overall_fit.legend`, so rewording a question in
  `jobmatch/matching/questions.py` reaches the UI with no frontend change.
- Filters threshold on `overall_fit_score`, never on `overall_fit_label`: the
  labels overlap in the real data (`weak` spans .13–.56, `good` .43–.62)
  because the label is the most probable level while the score is the
  expectation.
- PrimeVue 4.3+ renames `@primevue/themes` to `@primeuix/themes`; if you bump
  it, change the two imports in `src/theme.ts`.
