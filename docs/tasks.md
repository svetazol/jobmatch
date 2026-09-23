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

## Task 3 — matching

**Goal:** each stored vacancy gets a match result, and re-running costs nothing.

1. Move `cv_match/` → `jobmatch/matching/` (see architecture §8): `questions.py`
   unchanged, `sanitize.py` with `SystemExit` → `ValueError`, `matcher.py` →
   `jev.py`. Delete `cv_match/`.
2. Add the `MatchResult` and `LlmCall` models + migration.
3. `inputs_fingerprint()` over the canonicalised request; pin the model in config.
4. `match_vacancy()` writes its `LlmCall` row in its **own session** so the
   record survives the per-vacancy rollback, and returns it on the outcome.
5. `save_match()` supersedes the current row, then inserts.
6. Wire into the pipeline behind the `has_match()` check.

**Done when**

- [ ] Every fetched vacancy has a current match result
- [ ] A second run makes zero Jev calls and costs $0
- [ ] Editing one word in `data/cv.md` re-matches everything
- [ ] `SELECT sum(cost_usd) FROM llm_calls` tells you what it cost
- [ ] A failed Jev call leaves an `llm_calls` row with `status='error'` and the
      run continues

**Watch here:** this is the first task that spends money. Run it against 2–3
vacancies before the full 20, and check the actual per-call cost — it's the one
number the design couldn't guess.

---

## Task 4 — API (phase 2)

FastAPI over the same models. Read: ranked list (source / min fit / unseen
filters), vacancy detail. Write: triage (seen, star, hide).

- [ ] List orders by `overall_fit_score`, **never** by `overall_fit_label`
      (alphabetical would give `excellent, good, poor, strong, weak`) — assert
      this in a test
- [ ] Response models derive from the ORM classes; the schema is declared once

---

## Task 5 — frontend (phase 2)

Vue 3 + Vite SPA: ranked list with filters, detail view showing description,
skills and Jev's answers with confidences, triage buttons.

---

## Deferred, deliberately

Recorded so they're decisions rather than oversights.

| Thing | Revisit when |
|---|---|
| HTML search pagination for bulk backfill | The rolling window genuinely isn't enough after running on a schedule for a while |
| `is_qualified` threshold (0.5, a generated column) | You disagree with its verdicts. Changing it needs a hand-written migration — Alembic doesn't detect `Computed` changes |
| Re-fetch cadence (30d) and staleness hint (14d) | Arbitrary. Adjust once there's real data |
| `min_fit` label → threshold mapping | Task 4. Derive from Jev's stored `legend` rather than hardcoding |
| Retry/backoff on 429 | A 429 actually happens. §0 saw none, but that proves little |
| A second source | Whenever. It's a new file in `sources/` plus a config block — nothing else changes |
