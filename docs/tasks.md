# Implementation tasks

Ordered. Each task is shippable on its own and leaves the repo working.
Design lives in [architecture.md](architecture.md) — this file is the sequence,
not the reasoning.

Rule for all of them: **no per-vacancy files.** The database is the only store.

---

## Task 1 — scrape one real vacancy into Postgres — **done**

**Goal:** `jobmatch fetch <hh url>` puts a real hh.ru vacancy into a real
database table, and you can look at it and say "yes, that's the job."

Nothing about matching, no search, no loop over many vacancies. One vacancy,
end to end, so the schema meets real data before more is built on it.

**Steps**

1. ~~Dependencies~~ — `sqlalchemy`, `alembic`, `psycopg[binary]`, `requests`,
   `beautifulsoup4`, `lxml` are all in `pyproject.toml`; `pytest` is a dev dep.
2. ~~Database~~ — `docker compose up -d` runs Postgres 18 as
   `jobmatch-postgres` on host port **5433**; `DATABASE_URL` is in `.env`.
   (PG18 note: the volume mounts `/var/lib/postgresql`, *not* `.../data` — the
   older path makes the container crash-loop.)
3. ~~`jobmatch/models.py`~~ — `Base` + `Vacancy` only; `id` is `BigInteger`
   `Identity()`. `MatchResult`/`LlmCall` wait for task 3.
4. ~~`jobmatch/db.py`~~ — `engine` + `SessionLocal`, the only reader of
   `DATABASE_URL`.
5. ~~Alembic~~ — `migrations/env.py` imports `Base.metadata` and takes the URL
   from `jobmatch.db`, so there is still one reader of the env var.
   `compare_type=True`, `compare_server_default=True`; `alembic.ini` has no
   `sqlalchemy.url`. The `script.py.mako` tweak turned out to be unnecessary —
   Alembic 1.20 adds `from sqlalchemy.dialects import postgresql` itself when
   the diff needs it, and adding it to the template produced a duplicate import.
6. ~~First revision~~ — `d0f5719cd1b6_vacancies.py`, reviewed, `upgrade head` run.
7. ~~`jobmatch/sources/__init__.py`~~ — `VacancyData`, `Source`, `SOURCES`,
   `source_for_url()`.
8. ~~`jobmatch/sources/hh.py`~~ — `canonical_url`, `vacancy_id`, `parse`, `fetch`.
9. ~~`jobmatch/repository.py`~~ — `upsert_vacancy()`, one
   `ON CONFLICT (source, external_id) DO UPDATE ... RETURNING` statement.
10. ~~`jobmatch/cli.py`~~ — `jobmatch fetch <url>`, plus the `[project.scripts]`
    entry point.
11. ~~`tests/test_hh_parse.py`~~ — four offline tests over a synthetic page.
    A real 700KB fixture buys little over this and is a nuisance in git.

**Changed from the original plan** (all of it KISS, none of it structural):

- **`Source.fetch` takes a `url`, not a `Listing`.** Task 1 has no feed, so a
  `Listing` here would exist only to be constructed from a URL and immediately
  taken apart. `Listing` and `discover` arrive together in task 2, where the
  feed actually produces them.
- **`Source` gained `hosts`, and `sources/__init__.py` a `source_for_url()`.**
  `fetch <url>` has to decide *which* source owns the URL, and that decision is
  source knowledge — a tuple of hostnames per module keeps it there.
- **`VacancyData.extra` is called `raw`**, to match the column it becomes, and
  gained `published_at` (the JSON-LD `datePosted`).
- **`parse(html, url)` is split out of `fetch(url)`**, so the tests never touch
  the network.
- **`ix_vacancies_list` is deferred to task 4** with the query that needs it.

**Done when**

- [x] `jobmatch fetch https://hh.ru/vacancy/<id>` inserts exactly one row
- [x] Running it a second time updates that row — still one row, no duplicate
      (tracking params stripped, so the two URLs land on the same row)
- [x] `title`, `company`, `description`, `skills`, `published_at` are all populated
      and readable (no `\n`/`\xa0` debris in `salary_raw`)
- [x] `raw` holds the JSON-LD block
- [x] No `.md` or `.json` written anywhere

**What the real rows showed** (four vacancies fetched and read):

- `description` is clean plain text, 1.6–2.4k chars, no nbsp and no runs of
  blank lines. Good enough to send to Jev as-is — `_job_text()` in task 3 only
  has to frame it with the title, company and skills.
- HTML list bullets are lost (`<li>` becomes a bare line). Readable, and
  restoring them means walking the tree instead of `get_text()` — not worth it
  unless Jev's answers suggest otherwise.
- `salary` and `skills` are genuinely absent on some postings, so both stay
  nullable/empty rather than becoming a parse failure.
- The JSON-LD block is on every page, not just branded ones, and carries
  `datePosted`, `validThrough`, `identifier`, `jobLocation` and
  `applicantLocationRequirements`. Nothing in it is worth promoting to a column
  yet; `validThrough` is the one to watch if delisting needs a second signal.

---

## Task 2 — discovery, many vacancies — **done**

**Goal:** one command reads the search filter from config and stores every
vacancy it finds.

1. ~~`config.toml` + `jobmatch/config.py`~~ — `tomllib`, frozen `Settings` and
   `SourceConfig`. Params pass through verbatim, never inspected. **Georgia
   only for now:** `text = "python"`, `area = 28`, `work_format = "REMOTE"`,
   `search_field = ["name", "description"]`.
2. ~~`hh.discover()`~~ — RSS → `Listing`, `canonical_url()` on every link,
   `external_id` from `/vacancy/(\d+)`.
3. ~~Two-phase persistence~~ — `upsert_listing()` bumps `last_seen_at` and
   never touches `description`/`fetched_at`; `apply_fetched()` fills the rest.
   A stored `title`/`published_at` beats the feed's thinner version
   (`COALESCE`), and the page's `datePosted` beats the feed's `pubDate`.
4. ~~`jobmatch/pipeline.py`~~ — `run()` + `RunReport`, politeness delay only
   after a page hit.
5. ~~`fetch_attempts` / `fetch_error`~~ — counted on failure, capped by
   `max_fetch_attempts`; a 404 sets `delisted_at` instead and is never retried.

**Done when**

- [x] A run stores ~20 vacancies from the configured filter — 20 discovered,
      20 fetched, 38s wall clock at `fetch_delay = 1.0`
- [x] A second run immediately after costs one HTTP request and re-fetches
      nothing — `discovered 20, fetched 0, skipped 20` in 0.9s
- [x] A vacancy that 404s marks `delisted_at`; one that fails to parse
      increments `fetch_attempts` and doesn't kill the run — both covered by
      `tests/test_pipeline.py`, and hh.ru really does answer 404 for a dead id
      (checked: `/vacancy/1` and `/vacancy/999999999`), so the signal is real
      and not an archived-page 200

**Changed from the original plan**

- **The command is `jobmatch run`, not `jobmatch discover`.** It discovers
  *and* fetches, which is what the goal describes, and task 3 adds matching to
  the same loop — a command called `discover` would be a lie by then.
  Architecture §1 already named it `run`.
- **Two transactions per vacancy, not one.** The listing is committed *before*
  the network call. With a single transaction, a fetch that raises rolls back
  the listing too, and there is then no row to record `fetch_attempts` against
  — the counter could never reach its cap on a vacancy that fails on first
  sight. Discovery is cheap and always safe to commit; the fetch result is a
  separate write.
- **`VacancyGone` lives in `sources/__init__.py`.** The pipeline has to tell
  "employer took it down" (delist, never retry) from "something broke" (count
  it, try again next run), and it must do that without knowing what an HTTP
  status is. The source raises the distinction; the pipeline reads it.
- **`store_fetched()`** composes the two phases for `jobmatch fetch <url>`,
  which has no feed. It fetches first and derives the `Listing` from the
  result, so no source needs a "URL → Listing" function.
- **`count_vacancies()`** so `run` can print the corpus size — this is the
  rolling-window design's one visible reassurance that the corpus is growing.

**Notes from the first real runs**

- The `.ru` and `.ge` feeds with `area=28` returned *identical* id sets, so
  `RSS_URL` stays on hh.ru and the domain question is settled: it's cosmetic.
- The feed's `pubDate` is ISO 8601 with an offset (`2026-09-21T11:32:16.766+03:00`),
  not the RFC 822 string the RSS spec suggests — `fromisoformat` handles it,
  and `datePosted` from the page agrees with it.
- Of the first 25 rows: all fetched, all with `published_at`, 5 with no skills
  listed and only 6 with a salary. Both absences are normal, which is why
  neither is a parse failure.

Remember the feed is a **rolling 20-item window** (architecture §0). The corpus
grows by running repeatedly, not by paging.

**Georgia, and which domain (verified 2026-09-23).** `headhunter.ge` is the
Georgian HeadHunter — `georgia.hh.ru` redirects to it — and it is the same
engine as hh.ru: same RSS shape, same `data-qa` layout, same JSON-LD block,
and the *same vacancy ids*, so `parse()` needed no changes and a vacancy
reached through either domain is one row under `source = 'hh.ru'`. Both
domains are in `hh.HOSTS`.

The domain is **not** the country filter: `headhunter.ge/search/vacancy/rss?text=python`
returns Moscow vacancies. `area=28` (Грузия, per `api.hh.ru/areas`) is what
makes it Georgian — checked by reading `jobLocation.address.addressCountry`
from the JSON-LD of the top results, `GE/Тбилиси` with the param and
`RU/Москва` without. That is a config value, never a constant in `hh.py`.

Not a HeadHunter site, despite the name: **`hh.ge`** is a WordPress site
belonging to an unrelated recruitment agency. Don't add it to `HOSTS`.

---

## Task 3 — matching — **done**

**Goal:** each stored vacancy gets a match result, and re-running costs nothing.

1. ~~`cv_match/` → `jobmatch/matching/`~~ — `questions.py` plus a
   `QUESTIONS_HASH`, `sanitize.py` with `SystemExit` → `ValueError`,
   `matcher.py` → `jev.py`. `cv_match/` deleted.
2. ~~`MatchResult` and `LlmCall` models + migration~~ (`3406da5356e8`).
3. ~~`inputs_fingerprint()`~~ over the canonicalised request; model pinned in
   `config.toml` as `jev-1.13`.
4. ~~`match_vacancy()`~~ writes its `LlmCall` row in its **own** session, so
   the record survives the caller's rollback, and returns its id on the outcome.
5. ~~`save_match()`~~ supersedes the current row, then inserts.
6. ~~Wired into the pipeline~~ behind the fingerprint check.

**Done when**

- [x] Every fetched vacancy has a current match result — 31 of 31
- [x] A second run makes zero Jev calls and costs $0 — `matched 0 ($0.000000),
      already matched 31`
- [x] Editing one word in `data/cv.md` re-matches everything — verified by
      appending a line: the next run reported `already matched 0`
- [x] `SELECT sum(cost_usd) FROM llm_calls` tells you what it cost
- [x] A failed Jev call leaves an `llm_calls` row with `status='error'` and the
      run continues — `tests/test_matching.py`, `tests/test_pipeline.py`

**What the live API actually returns** — three findings the design guessed wrong:

- **`score` is not 0–1.** Jev returns the expected position on the legend's own
  index scale: `2.4` on a 0..4 rubric. Architecture §5 assumed 0–1 and §10 put
  a `CHECK (overall_fit_score BETWEEN 0 AND 1)` on the column — **that
  constraint would have rejected every row.** `overall_fit_score` is now stored
  normalised (`score / (levels - 1)`), which keeps the sort key and any
  `min_fit` threshold meaningful across question sets with different numbers of
  levels; the raw value stays in `answers`. The check constraint stands.
- **`cost_usd` needed more precision.** A call costs `$0.000413616`.
  `Numeric(12,6)` — architecture §3.3 — rounds that to `$0.000414`, and a
  cheaper call to zero. Widened to `Numeric(14,9)`.
- **The SDK drops `cost`, `id` and `provider`.** `SystemOneResponse` models
  only `model`, `usage.input_tokens/output_tokens` and `answers`; the rest is
  in the JSON body, reachable through the public `raw_http_response`. Without
  that, `llm_calls` would have no cost column worth summing.

**Changed from the original plan**

- **Matching walks the table, not the discovery loop.** The architecture's
  `run()` matches inside the per-listing loop. That silently fails the first
  criterion: the feed is a rolling 20-item window, so a vacancy stored last
  week is never revisited, and "editing the CV re-matches everything" would
  mean "re-matches the last twenty". `run()` is now two phases — discover and
  fetch from the feed, then match from `matchable_vacancy_ids()`, scoped to
  the sources config currently enables.
- **`make_current()` replaced `has_match()`.** A bare existence check skips a
  fingerprint it already has, which is right, but it leaves the *wrong* row
  flagged current: revert the CV, and the answer for the reverted CV is found
  and skipped while the newer row keeps `superseded_at IS NULL`. Architecture
  §4 promises the old row is "un-superseded"; nothing in the plan actually did
  it. `make_current()` returns the same boolean and revives the row when it is
  stale. Verified: reverting the CV restored match #4 as current, left #32
  superseded, and `count(llm_calls)` did not move.
- **`--limit N` on `run`** caps the matching phase — the phase that costs
  money. Discovery and fetch always finish; they're bounded by the window.
- **The label is the most probable level, not the rounded score.** With `2.4`
  and probabilities `{good: 0.47, strong: 0.43}`, rounding agrees by luck;
  with `2.6` it would not. The score remains the sort key either way.
- **`job_text()` omits fields the posting lacks.** 5 of the first 25 vacancies
  had no skills and only 6 had a salary; an empty `Salary:` line is noise the
  model would have to interpret.

**What it costs.** 32 calls, **$0.0133** total — about **$0.00042 per vacancy**,
~3.2s each. Input is ~10k tokens per call and the sanitized CV (34.6k chars) is
almost all of it, so cost scales with CV length, not with the vacancy. A full
20-vacancy run is under a cent. The design's caution about spend was
unwarranted at this scale, but `llm_calls` is what proves it rather than
assuming it.

**On the answers themselves:** 31 vacancies scored 0.00–0.58, no `strong` or
`excellent`, and `technical_skills` is the top gap almost everywhere. Worth a
look at whether that's the CV, the filter (`text=python` against a CV that may
not be a Python engineer's), or a rubric that needs sharper criteria — a
question for the data, not the code.

---

## Task 4 — country and work format on the row — **done**

**Goal:** each vacancy records which country it is in and whether it is remote,
on-site, hybrid — or any combination of those. Discovery can cover every hh
country; it is pointed at Georgia alone for now.

**Verified against the live site (2026-09-24)**, sampling areas 28, 113, 40, 16
and 1001 before any code was written.

- All nine codes are valid and are the *complete* top-level area list from
  `api.hh.ru/areas`: 113 Russia, 5 Ukraine, 40 Kazakhstan, 9 Azerbaijan,
  16 Belarus, 28 Georgia, 48 Kyrgyzstan, 97 Uzbekistan, 1001 other regions.
- **The searched area is the wrong thing to store.** Area 1001 is a catch-all
  that returns real countries — the sample produced Serbia (`RS`) and Cyprus
  (`CY`). Storing "other regions" on those rows would discard what the page
  already knows, so `country` is read from the posting.
- **Every sampled page carries the country twice:** JSON-LD
  `applicantLocationRequirements.name` (hh's own localised label) and
  `jobLocation.address.addressCountry` (ISO alpha-2). They agreed with the
  searched area every time. Both were already inside `vacancies.raw`.
- **Belarus needs no special handling**, despite hh running it as `rabota.by`:
  the area 16 feed returns 20 items whose links are all on `hh.ru`, and the
  pages resolve to `BY` like any other. No second domain in `HOSTS`. (Checked
  because a source's own regional domain is exactly where this would break.)
- **Work format is `data-qa="work-formats-text"`**, not JSON-LD
  (`jobLocationType` and `employmentType` were `null` on every sample). It is
  one line naming up to three formats, separated by commas and "or".

**What was built**

1. `hh.parse()` fills two new `VacancyData` fields: `country` and
   `work_formats: list[str]`, normalised to `onsite` / `remote` / `hybrid` /
   `field_work`. An unrecognised phrase is **kept verbatim, not dropped** —
   same reasoning as the no-CHECK-constraint rule in architecture §11: an
   unexpected value is a data-quality signal, and discarding it turns a
   visible surprise into a mystery.
2. `Vacancy.country` (text, nullable) and `Vacancy.work_formats` (`text[]`,
   default `'{}'`), migration `582ff1f02df1`.
3. `hh.discover()` accepts `area` as a **list** and issues one request per
   entry. The 20-item cap is per request, so several areas in one request
   would still return 20 items in total. This stays inside the seam: params
   still reach the source verbatim, and "how many requests this filter costs"
   is the same class of site knowledge as pagination (architecture §2).
   Config currently passes a single area, 28.
4. The `work_format = "REMOTE"` filter was **dropped** from the search. With
   it the new column is nearly constant; without it the column is what you
   filter on afterwards, which is the point of having it.

**Done when**

- [x] `country` populated on every fetched row
- [x] `work_formats` holds a genuine *set* — of the first 20 rows: 12
      `{remote}`, 2 `{onsite,remote}`, 2 `{onsite,hybrid}`, 2
      `{onsite,remote,hybrid}`, 1 `{hybrid}`, 1 `{onsite}`. Dropping the REMOTE
      filter was worth it: 8 of 20 would have been invisible
- [x] A second run re-fetches nothing and pays nothing for rows it already had
- [x] Parse tests cover a multi-value format, an unrecognised one, a posting
      with no format at all, an unmapped country and a missing country

**Decisions taken during the work**

- **Country is stored as an English name**, mapped from the ISO code via
  `hh.COUNTRY_NAMES` — the nine hh countries. A country outside that map (only
  reachable through area 1001) is stored as its bare ISO code: visible and
  obviously unmapped, rather than silently wrong. `country` is NULL when the
  posting carries no ISO code at all; hh's own localised name stays in `raw`.
- **Georgia and Belarus.** `config.toml` passes `area = [28, 16]`; the
  fan-out issues one feed request per area. Widening further is a config edit,
  no code change — adding Belarus took exactly one, and the first run brought
  in 20 Belarusian vacancies with `country = 'Belarus'` resolved from the
  page's ISO code, no `rabota.by` handling needed.

  Worth noting from that run: only **1 of 20** Belarusian postings offers
  remote, against 18 of 22 Georgian ones. Whatever the reason, it is the kind
  of thing the `work_formats` column exists to make visible rather than
  guessable.
- **The database was wiped and re-scraped**, rather than backfilled. `country`
  *could* have come from the JSON-LD already in `raw`, but only in the site's
  own language, and `work_formats` comes from page markup that was never
  stored — so the migration adds columns and nothing else. Cost of the reset:
  20 re-matches, $0.0082.
- **No Russian in comments.** The only Russian left in the codebase is in
  `hh.WORK_FORMATS`, whose keys are the site's own wording and have to be
  spelled the way it spells them, and in the test fixtures that imitate a real
  page.
- **QA, AQA, testing and devops postings are excluded at the source**, via
  `excluded_text = '"QA","AQA",тестировщик,devops'` in the filter (verified
  against the live feed: 20 items before, a different 20 after). hh checks
  `excluded_text` against the same fields as `search_field`, so it also drops a
  posting whose *description* is QA work even when the title reads otherwise —
  "AI Agent Developer" and "Middle Technical Writer" both went that way.
  Quoted terms are exact phrases.

  The nine stored rows the new filter would have excluded were deleted with
  the query below, which mirrors those semantics. Deleting a vacancy cascades
  its `match_results` and only *nulls* `llm_calls.vacancy_id`, so the spend
  ledger stayed whole — 29 calls, $0.0119, nine of them now orphaned.

  ```sql
  DELETE FROM vacancies
  WHERE title ~* '(^|[^a-z])(qa|aqa|devops)([^a-z]|$)' OR title ~* 'тестировщик'
     OR description ~* '(^|[^a-z])(qa|aqa|devops)([^a-z]|$)' OR description ~* 'тестировщик';
  ```

  Worth knowing: **changing `excluded_text` later does not retroactively clean
  the table.** Already-stored rows are never re-discovered, so nothing removes
  them; that query is the manual step, and it is the reason it is written down
  here rather than buried in a shell history.

## Task 5 — HTML search pagination, because RSS isn't enough

**Goal:** discovery reads the paginated HTML search results instead of the RSS
feed, so a run sees far more than the newest 20 per area.

This was in *Deferred, deliberately* — "the rolling window genuinely isn't
enough". It now isn't: the same filter that RSS answers with 40 vacancies
(20 per area) reports **193** on the search page.

**It fits the existing seam exactly.** `discover()` is source-owned, and
architecture §0 already called this shot: *"hh.ru's HTML search results page
*is* paginated — that would be a second function inside `sources/hh.py` and
nothing outside it would change."* Nothing does: `Listing`, `Source`,
`pipeline.run()`, the two-phase upsert and the whole matching layer are
untouched. This task is one new function, one deleted one, and config.

**Verified against the live site (2026-09-24)** before planning:

- The results page is **server-rendered** — no JavaScript needed. Vacancy
  links are plain `<a href=".../vacancy/123">`; the count is
  `data-qa="vacancies-search-header"` ("Найдено 193 вакансии").
- **20 results per page**, `page=0,1,2,…`. `items_on_page` is ignored: asking
  for 100 still returns 20.
- Of the browser URL's parameters, only `text`, `area` (repeatable),
  `search_field` (repeatable), `excluded_text` and `page` matter.
  `hhtmFrom`, `hhtmFromLabel`, `hhtmSource`, `hhtmSourceLabel`,
  `L_save_area` and `enable_snippets` are UI and tracking noise — drop them.
- **There is a depth cap of about 4 pages (~80 results) per query**, well
  below the 193 the header advertises. Page 4 onward returns a page that has
  the header but no vacancies. Splitting by area gets further: Georgia alone
  reports 37, Belarus alone 155.

**The one trap, and the reason this task is not trivial**

An empty results page and a throttled response **look the same to
`raise_for_status()`** — both are HTTP 200. Under fast paging hh intermittently
serves a stripped ~629KB page carrying "Для работы с нашим сайтом необходимо,
чтобы Вы включили JavaScript"; it has no results *and no search header*. A
naive `while page_has_items` loop would treat that as the end of the results
and silently stop early, at a different point every run — the worst kind of
bug, because the run reports success.

They are distinguishable, and the parser must do it:

| Response | `vacancies-search-header` | Vacancies | Meaning |
|---|---|---|---|
| ~1.2MB | present | 20 | a good page |
| ~629KB | **present** | 0 | genuinely past the end — stop |
| ~629KB | **absent** | 0 | throttled — sleep and retry, do *not* stop |

Observed directly: page 2 returned the JS-wall page at a 1s delay and the full
20 results on retry at 3s.

**Steps**

1. `hh.discover()` walks pages instead of reading RSS: `page=0`, then `+1`,
   until a page with the header holds no vacancies, or a page budget is hit.
   Stop conditions are explicit — never "the page looked empty".
2. A degraded page is a retry, with a short backoff, then a raised error if it
   persists. An error here must not look like "no more results".
3. Keep the per-area fan-out from task 4. It is now doing double duty: it
   works around the depth cap as well as the per-request result cap, and the
   numbers above say it roughly doubles the reachable corpus.
4. `max_pages` in `config.toml` (default ~5, one past the observed cap) so a
   filter that matches thousands cannot walk forever.
5. Politeness: search pages are ~1.2MB and throttling is real at 1s. Use a
   separate, longer delay than `fetch_delay` — 2–3s — and say so in config.
6. Set `order_by=publication_time` in the filter params. HTML search defaults
   to relevance order, where RSS was newest-first; with a depth cap, *which*
   80 you get matters. Confirm it is honoured before relying on it.
7. **Delete the RSS path.** Two discovery mechanisms would be two things to
   keep working for one job, and HTML is a superset once ordered by date. The
   `_feed()` helper and `RSS_URL` go; `discover()` keeps its signature, so
   nothing outside `hh.py` notices.
8. Tests off a saved fixture: a good page, an end page (header, no items), a
   throttled page (no header) — asserting the third raises or retries rather
   than ending the walk.

**Done when**

- [ ] One run discovers ≥150 vacancies for the current two-area filter, against
      40 today
- [ ] Re-running discovers the same set and fetches nothing new
- [ ] A throttled page mid-walk does not truncate the run — forced in a test
- [ ] `max_pages` is respected and logged when hit, so a truncated crawl is
      visible rather than silent
- [ ] Nothing outside `jobmatch/sources/hh.py` and `config.toml` changed, which
      is the point: this is the seam's first real test

**Cost.** Discovery gets slower (~10 page loads at 2–3s versus one RSS call),
but that is HTTP, not money. The spend is in what it finds: ~150 new vacancies
at $0.00042 is about **$0.06** for the first full run, then near zero.

**Deferred from this task, deliberately**

| Thing | Revisit when |
|---|---|
| Slicing the query further (by date posted, sub-region, salary band) to beat the ~80-per-query cap | The per-area split stops being enough — Belarus alone already exceeds it at 155 |
| Retry/backoff as a general policy | It is local to the search walk here. If fetching starts throttling too, lift it out then |

---

## Task 6 — API (phase 2)

FastAPI over the same models. Read: ranked list (source / min fit / unseen
filters), vacancy detail. Write: triage (seen, star, hide).

- [ ] List orders by `overall_fit_score`, **never** by `overall_fit_label`
      (alphabetical would give `excellent, good, poor, strong, weak`) — assert
      this in a test
- [ ] Response models derive from the ORM classes; the schema is declared once

---

## Task 7 — frontend (phase 2)

Vue 3 + Vite SPA: ranked list with filters, detail view showing description,
skills and Jev's answers with confidences, triage buttons.

---

## Deferred, deliberately

Recorded so they're decisions rather than oversights.

| Thing | Revisit when |
|---|---|
| ~~HTML search pagination for bulk backfill~~ | **Now task 5** — the window wasn't enough: 40 via RSS against 193 on the search page |
| `is_qualified` threshold (0.5, a generated column) | You disagree with its verdicts. Changing it needs a hand-written migration — Alembic doesn't detect `Computed` changes |
| Re-fetch cadence (30d) and staleness hint (14d) | Arbitrary. Adjust once there's real data |
| `min_fit` label → threshold mapping | Task 6. Derive from Jev's stored `legend` rather than hardcoding |
| Retry/backoff on 429 | A 429 actually happens. §0 saw none, but that proves little |
| A second source | Whenever. It's a new file in `sources/` plus a config block — nothing else changes |
