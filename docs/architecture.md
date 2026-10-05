# Architecture

Why the code is shaped the way it is. The companion documents:

- `docs/pipeline.md` — the three phases, and what is on disk after each
- `docs/decisions.md` — what was considered and refused
- `jobmatch/models.py` — the schema itself, with the reason for each column

This file holds the reasoning that none of those can: the seams, the two
idempotency rules, what the timestamps mean, and where the hard edges are.

Nothing here restates a value that lives in code. hh's measured limits are in
`jobmatch/sources/hh/`; the config's contents are in `config.toml`.

---

## 1. Layout

Three leaf areas that know nothing about each other — `sources/` (the web),
`matching/` (the LLM), `models.py`+`repository.py` (the DB) — and one module,
`pipeline.py`, allowed to know all three. `cli.py` and later `api/` are both
thin callers of that same core.

No `src/` layout (buys import hygiene that matters for distributed libraries,
not for one app) and no `core/` subpackage (a path segment, nothing more).
`api/` lives inside the same package because `initial_task.md` rules out a second
codebase reading the DB: FastAPI imports `jobmatch.models` rather than
re-declaring anything. Nothing in phase 1 moves when it appears, because
`models.py` and `repository.py` already have no CLI knowledge.

---

## 2. The source seam

A source answers exactly two questions and owns everything else — base URL,
RSS-vs-JSON-vs-HTML, pagination, headers, parsing, id extraction:

`country` and `work_formats` are columns rather than `raw` lookups because the
UI filters on both. `work_formats` is a list, not one value: a real posting can
offer on-site, remote and hybrid at once.

`discover` yields as much as the crawl cap allows; hh serves roughly four pages
deep per query, and `max_pages = 5` is one past that so hitting the cap means
the depth moved and is worth a look. The pipeline neither knows nor cares how
many pages that was — duplicates across runs are absorbed entirely by the
upsert.

Params go from config to the source **verbatim** and are never inspected in
between. hh.ru's repeated keys (`search_field=name&search_field=description`)
are a TOML list that `httpx2` expands; a JSON-API source would read the same
dict as a POST body. The pipeline cannot tell the difference.

`Source` is a three-field frozen dataclass rather than an ABC — it exists only
because `SOURCES` needs a value type. `SOURCES` is a dict literal rather than
entry-point autodiscovery: config names a source by string, this maps the
string to code, and with 1–4 sources an import line is the whole mechanism.

---

## 3. Database

Three tables: what was scraped, what the AI concluded, and what each AI call
cost. Single user kills `users`, `user_vacancy_state`, roles. Also deliberately
absent: a `sources` table (sources are config), a `crawl_log` of discovery runs
(nothing reads it), `skills`/`companies` tables (nothing joins on them).

```mermaid
erDiagram
    VACANCIES ||--o{ LLM_CALLS : "was asked about"
    VACANCIES ||--o{ MATCH_RESULTS : "has"
    LLM_CALLS ||--o| MATCH_RESULTS : "produced"

    VACANCIES {
        bigint id PK
        text source UK "half the natural key"
        text external_id UK "the source's own id"
        text url
        text title "null until fetched"
        text company
        text salary_raw "unparsed, as shown"
        text experience_raw "unparsed, as shown"
        text description "null until fetched"
        text_array skills
        timestamptz published_at "from the source, when given"
        text country "the posting's own name for it"
        text_array work_formats "onsite/remote/hybrid; a set, not one value"
        jsonb raw "source-specific payload"
        char64 content_hash "sha256 of the job text sent to Jev"
        timestamptz first_seen_at
        timestamptz last_seen_at "discovery only ever bumps this"
        timestamptz fetched_at "NULL = the fetch queue"
        smallint fetch_attempts "caps retries on broken pages"
        text fetch_error
        timestamptz delisted_at "only on a positive 404/archived signal"
        timestamptz seen_at "triage"
        timestamptz starred_at "triage"
        timestamptz hidden_at "triage"
        timestamptz created_at
        timestamptz updated_at
    }

    MATCH_RESULTS {
        bigint id PK
        bigint vacancy_id FK
        bigint llm_call_id FK "the call that produced it"
        char64 inputs_fingerprint UK "THE idempotency key"
        char64 vacancy_content_hash "diagnostic"
        char64 cv_hash "diagnostic"
        char64 questions_hash "diagnostic + label vocabulary"
        float is_qualified_noul "raw Noul 0-1"
        boolean is_qualified "GENERATED: noul >= 0.5"
        float overall_fit_score "THE sort key, 0-1"
        text overall_fit_label "display only, never ORDER BY"
        float overall_fit_confidence
        text top_gap
        float top_gap_confidence
        text best_angle "which CV pitch to lead with"
        float best_angle_confidence
        jsonb answers "complete response; columns are projections"
        timestamptz created_at
        timestamptz superseded_at "NULL = current"
    }

    LLM_CALLS {
        bigint id PK
        bigint vacancy_id FK "SET NULL: spend history outlives the vacancy"
        char64 inputs_fingerprint "what was asked, even if it failed"
        text model_requested "in the fingerprint"
        text model_resolved "what actually answered"
        text provider
        text response_id "audit trail"
        text status "ok | error"
        smallint http_status
        text error_type "exception class"
        text error_message
        int input_tokens
        int output_tokens
        numeric cost_usd
        int duration_ms
        timestamptz started_at
        timestamptz finished_at
    }
```

### 3.1 `vacancies`

One row per vacancy per source. Discovery and fetch are two phases, so a
partially-filled row is a first-class, indexable state — not a second table.

Columns, types and the reason for each: `jobmatch/models.py`, which is the
schema's source of truth and carries that reasoning as comments.

Triage is three nullable timestamps rather than booleans: same filter cost
(`seen_at IS NULL`), "when" for free, one consistent style, and no left join on
the hottest query. `fetch_attempts`/`fetch_error` earn their place because
without them every run re-fetches the same broken page forever — which is the
"cheap to re-run" requirement, not a hypothetical.

### 3.2 `match_results`

One row per *paid* Jev call. Append-only; the only update is setting
`superseded_at`.

Columns and the reason for each: `jobmatch/models.py`.

This table holds *conclusions*. Everything operational about the call that
produced them — model, provider, tokens, cost, how long it took, whether it
blew up — lives in `llm_calls`.

### 3.3 `llm_calls`

One row per Jev call **attempted** — including the ones that failed. This is
the operational ledger: what was asked, what it cost, how long it took, and
what went wrong.

`match_results` only ever records a call that succeeded *and* parsed. Without
this table, a run where a third of the calls 429'd leaves no evidence at all,
and a call that cost money but returned an unparseable body is invisible.

Columns and the reason for each: `jobmatch/models.py`.

The questions this answers, each a one-liner:

**Why a separate table rather than more columns on `match_results`:** the two
have different lifetimes and different row counts. A `match_results` row is a
conclusion that gets superseded when inputs change; an `llm_calls` row is a
historical fact that is never revised and exists even when there is no
conclusion. Failed calls have no result to hang columns on, and that's exactly
the case you most want recorded.

**What it deliberately is not:** a retry queue or a dead-letter table. Nothing
reads it to decide what to do next — the pipeline still works out what's
missing from `match_results` and `fetched_at`, as in §7. This is for you to
read, not for the program to branch on.

**One gap worth naming:** dedupe keys off `match_results`, so a call that
*succeeded and was billed* but whose response failed to parse will be paid for
again on the next run. `llm_calls` makes that visible (the last query above)
rather than fixing it. Fix it only if it actually happens.

### 3.4 Keys and indexes

`llm_calls` gets **no unique constraint**. It is an append-only log of things
that happened; two identical attempts are two facts, not a conflict. The
no-double-pay rule lives on `match_results`, where it belongs.

The key is `(source, external_id)`, **not `url`**: URLs carry tracking params,
differ between the feed and the canonical page, and change when a site rebrands
its routes. A source that genuinely has no id synthesises one deterministically
inside its own module.

Re-scrape is one statement, no read-modify-write race — and it deliberately
never touches `description`, `content_hash` or `fetched_at`, which belong to
the fetch phase:

An earlier draft of this section also promised
`ix_vacancies_list (source, seen_at, last_seen_at DESC)`. It was never created,
and on reflection it should not be: the list is ordered by
`overall_fit_score`, which lives on the other table, so that index could never
serve the sort it was invented for. `ix_match_results_current_rank` is the one
that matters.

Canonical phase-2 list query — a LEFT JOIN, because a fetched-but-unmatched
vacancy is a real state the UI shows rather than a row to drop:

`min_fit` is a threshold on the **score**, never on the label: verified against
the real corpus, the labels overlap (`weak` spans .13–.56, `good` .43–.62)
because the label is the most probable level while the score is the
expectation. `label = 'strong'` is not a range and never will be.

`fit_probabilities` is projected out of `answers` rather than promoted to a
column: five floats the list draws as a histogram, read on every page load but
never sorted or filtered on — which is exactly the §5 rule for staying in JSONB.

Honest caveat: at hundreds-to-low-thousands of rows Postgres will seq-scan
regardless and finish in under a millisecond. The three **unique** indexes are
what matter; the rest are cheap insurance. No GIN on `skills` or `raw` — nothing
filters by skill or queries inside `raw`.

---

## 4. Never pay for the same match twice

Fingerprint the **request**, not the row. Before calling Jev, build the exact
request and hash its canonical serialisation:

```python
def inputs_fingerprint(cv_sanitized: str, job_text: str, questions: dict, model: str) -> str:
    payload = {
        "state": {"cv": cv_sanitized, "job_description": job_text},
        "questions": {k: q.model_dump(mode="json") for k, q in sorted(questions.items())},
        "model": model,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
```

Hit on `(vacancy_id, inputs_fingerprint)` → zero cost, and if that row was
superseded it is made current again (`repository.make_current`) — a hit must
not leave a row computed from *different* inputs flagged as current. Miss →
call, supersede the current row, insert. The unique constraint keeps this correct
across a crash mid-run or two concurrent processes: the loser gets an
`IntegrityError` and swallows it.

Everything that should invalidate flows through that one hash, because the hash
covers the whole request:

| Change | Why it invalidates |
|---|---|
| Employer edits the posting | `job_description` differs. `content_hash <> old` is the cheap pre-check before even building the CV side. |
| CV rewritten | `cv` differs. |
| `sanitize.py` changed | The hash is over the **sanitized** output, so a new redaction rule invalidates. Hashing the file on disk would miss this. |
| A question added, removed or reworded | `questions` are in the hash. No hand-maintained `PROMPT_VERSION` to forget to bump. |
| Model pinned differently | `model` is in the hash. |
| Model upgraded | Caught, **because the model is pinned**. A bump is a config edit, which changes the fingerprint and re-matches cleanly. This is exactly why `jev-latest` is not used: with it, a silent server-side roll would be invisible — you cannot know the resolved model before paying for the call. |

The three component hashes are not used for lookup. They cost 3 × 64 bytes and
let you answer "which vacancies are stale only because I edited my CV?" without
recomputing anything.

A pleasant consequence of append-only + that unique constraint: revert the CV
to an earlier version and the old match row is *found*, not re-paid — you
un-supersede it.

---

## 5. Storing Jev's answers: hybrid, not either/or

Typed columns for the four promoted answers, `answers` JSONB for the complete
record. The split rule: **a column is promoted only if the UI sorts, filters or
lists by it.**

Not pure JSONB, because the list sorts by fit on every page load;
`(answers->'overall_fit'->>'score')::float` in an `ORDER BY` means a seq-scan
sort or a brittle expression index, it leaks the response shape into the query
layer, and JSONB gives no `NOT NULL` — an SDK shape change would silently write
rows of nulls that nothing complains about.

Not pure columns, because the question set will change: every added question
becomes 2–3 columns and a migration, every removal leaves dead ones, and
`probabilities`/`legend` are variable-width by nature.

**Sorting is correct for free.** Jev's `Score` returns a position on the
ordered scale — verified 2026-09-23: the *raw* value is on the legend's own
index scale (`2.4` of `0..4`), not 0–1, so `matching/jev.py` normalises it to
0–1 before storing and keeps the raw value in `answers`. The stored score is
therefore a plain float, so `ORDER BY overall_fit_score DESC` is an
indexed float sort. The classic enum trap (`excellent < good < poor < strong <
weak` alphabetically) never arises because the label is never the sort key.
Worth a comment in the model and a test asserting the list endpoint orders by
score. `min_fit` is a float comparison; the API maps a UI label to a threshold
in one place, ideally derived from the stored `legend` rather than hardcoded.

The label is the **most probable single level**, not the rounded expectation —
with 0.47 on "good" and 0.43 on "strong", rounding the score would agree by
luck and disagree just as easily. That is why the two are stored separately and
why the label is display-only.

When a question is added it lives in `answers` only, until the UI wants to sort
on it — at which point you add a column and backfill from JSONB in the same
Alembic revision. That backfill is only possible because the full JSONB is
kept, which is the main argument for the hybrid. Rows with a different
`questions_hash` answered different questions and shouldn't be ranked against
each other; in practice `superseded_at IS NULL` handles that, but the column is
there to explain a discontinuity in scores.

---

## 6. Timestamps, archival, vanishing vacancies

All `timestamptz`, UTC, `server_default=func.now()` so the DB clock is
authoritative and pipeline and API can't disagree. `updated_at` uses
SQLAlchemy `onupdate`, not a trigger — both writers go through these models,
which is the point of the shared-repo decision.

Four distinct roles, deliberately not merged: `created_at` (row appeared here),
`first_seen_at`/`last_seen_at` (discovery saw it), `published_at` (the source
says it was posted), `fetched_at` (detail last parsed).

**No soft delete.** Nothing deletes a vacancy, and the two things soft-delete
usually covers already have meaningful columns: `hidden_at` (the user doesn't
want to see it) and `delisted_at` (the employer took it down). Collapsing them
loses a distinction the UI needs — a starred vacancy that got delisted should
still show, with a "no longer listed" badge.

**Absence from search is not closure.** A vacancy dropping out of the feed can
mean it closed, but just as often means the feed paginated differently, ranking
shifted, or you edited the filter params. So: discovery only ever bumps
`last_seen_at`; absence writes nothing. `delisted_at` is set only by a positive
signal from a direct fetch (404/410, archived-vacancy page). Staleness in the
UI is derived (`now() - last_seen_at > interval '14 days'`), not stored — a
stored flag would need a job to maintain it and be wrong between runs. Nothing
is purged.

Occasionally re-fetch already-fetched vacancies still appearing in search
(`fetched_at < now() - interval '30 days'`) so employer edits get noticed. The
new `content_hash` differs, the fingerprint changes, the match re-runs — which
falls out of §4 with no extra mechanism.

---

## 7. Pipeline

Phase by phase, with what is on disk after each one and what a re-run
can skip: `docs/pipeline.md`.

The design property worth stating here is that failure isolation is
*structural*, not policy. Each stage of each vacancy is its own
transaction, so one bad page or one failed API call costs a warning
rather than the run — and nothing records progress, because every guard
is a condition on the rows.

## 8. Matcher integration

Importable as a plain function; no subprocess anywhere. (The cover note's
`claude -p` is not the matcher and stores nothing; see `cover_note.py`.) `cv_match/` is absorbed
into `jobmatch/matching/` and deleted:

- `questions.py` → moved unchanged, plus a module-level `QUESTIONS_HASH`.
- `sanitize.py` → moved, `SystemExit` → `ValueError` (a library must not kill
  the process; `cli.py` decides exit codes).
- `matcher.py` → `matching/jev.py`, widened to own the whole LLM concern.
- `config.py` → folded into `jobmatch/config.py`; `JOB_PATH` disappears, jobs
  come from the DB.
- `cli.py` → **deleted**; its behaviour survives as `jobmatch match --url <url>`.

Public surface:

```python
def open_client(api_key: str | None = None) -> AbstractContextManager[TypeSafeClient]
def load_cv(path: Path | str) -> str                # read + sanitize_cv
def sha256(text: str) -> str                        # content_hash is an alias of it
def job_text(vacancy: Vacancy) -> str               # renders the posting for the model
def inputs_fingerprint(cv, job, questions, model) -> str
def preview_vacancy(client, cv, vacancy, model, *, job) -> Preview   # asks, records nothing
def match_vacancy(client, cv, vacancy, model, *,
                  fingerprint, cv_hash, content_hash, job) -> MatchOutcome
```

The hashes are passed **in** rather than computed inside `match_vacancy`: the
caller has already built the fingerprint to decide whether to call at all, and
recomputing it would be a second chance to disagree with itself.

`MatchOutcome` is a small frozen dataclass holding the flattened fields, the
full `answers: dict`, and `call_id: int` — the id of an `llm_calls` row that is
*already committed*, in its own session, before the outcome is returned. Not
the ORM object: the row must survive the caller's rollback, so it cannot be
attached to the caller's session. `Preview` is the same shape minus the
identity of a stored call, which is what makes `--dry-run` able to report
exactly what a real run would have saved.

`jev.py` is the only place that knows the SDK's answer shape
(`answers.is_qualified.noul`, `answers.overall_fit.score` + `legend`,
`answers.top_gap.choice`, `answers.best_angle.choice`, `usage.cost`) — that
containment is what lets `repository.save_match` stay a dumb column write.

Two deliberate choices: the client is opened **once per run** (it's an HTTP
session), and the CV is loaded and sanitized **once per run**, not per vacancy.
`_job_text(vacancy)` renders the vacancy for the model, mirroring the
prototype's `to_markdown()` — the one piece of it worth keeping. It goes to the
model, never to disk.

---

## 9. Config

**Code:** questions, parsing, base URLs, headers, the schema.
**Config:** which sources are enabled, their opaque params, model, CV path, fetch knobs.
**Secrets (`.env`):** `DATABASE_URL`, `OPENROUTER_API_KEY`, with `.env.example`
checked in as the template. The database is this repo's own container, so
`DATABASE_URL` is a localhost DSN on port 5433:

```
DATABASE_URL=postgresql+psycopg://jobmatch:jobmatch@localhost:5433/jobmatch
```

The `+psycopg` suffix is required — without it SQLAlchemy reaches for psycopg2,
which isn't installed. The repo owning its own container is what makes "the
database is the only store" safe: nothing here depends on a container another
project can tear down.

`config.toml`, read with stdlib `tomllib` — no YAML dependency, no
pydantic-settings, and unlike JSON it takes comments.

It holds **choices only**. hh's crawl ceiling and its safe fetch rate are
properties of the site, measured against it, and live beside the code that
measured them — `CRAWL` and `RATE` in `sources/hh/__init__.py`, carried on
`Source`. A `[crawl]` block or a `fetch_workers` line in config is an override
for a deliberate experiment, never a requirement; omit them and the source's own
values are used. The rule: a setting a config *must* restate is a setting a
config can contradict — see `docs/decisions.md`.

```toml
model = "jev-1.13"       # pinned, never an alias — see §4
cv_path = "data/cv2.md"  # what the *next* match run uses
stats_cv_hash = "9e5b…"  # what has *already* been matched — a different thing

[sources."hh.ru".search]           # opaque: passed to the source verbatim
text = "python"
excluded_text = '"QA","AQA",тестировщик,devops'
search_field = ["name", "description"]

[sources."hh.ru".sweeps]           # `run --only bg` crawls just that one
bg = ["belarus", "georgia"]
ru = ["russia"]
```

A country name is the whole "where". `areas.coverage()` expands it into the
queries needed to get under hh's ceiling — one for Belarus or Georgia, 92 for
Russia (88 regions, plus Moscow four times by `experience` because it busts the
cap alone and has no child areas). `python -m jobmatch.sources.hh russia` prints
them. The area ids come from `areas.json`, generated from `api.hh.ru/areas` by
`_refresh.py`, because 88 integers are the answer to one API call rather than
authored knowledge.

`search` is nested under the source, not shared across sources: each site has its
own query vocabulary, and one `search` per source makes it structurally
impossible for two sweeps to drift apart — which is what a second config file
did. `search` is never inspected on the way through; `stats.py::_search()` is the
one sanctioned exception, reading `text`/`search_field`/`excluded_text` to
*describe* the corpus on the market page.

Validation in `load_settings()`, all of it fatal at load rather than mid-crawl:
the file parses; unknown top-level settings are named (`fetch_worker` silently
ran one worker before); every source name is in `SOURCES`; a stray key under a
source is refused (notably `params`, what `search` used to be called); every
sweep is a non-empty list; and every country name is in `areas.COUNTRIES`, so a
typo fails before the first request instead of after the earlier sweeps have
spent theirs.

Alembic reads `DATABASE_URL` from the environment in `migrations/env.py`, not
from `alembic.ini`, so pipeline, API and migrations share one connection string.

---

## 10. Alembic and enums

```python
# migrations/env.py
from jobmatch.models import Base
target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
context.configure(connection=connection, target_metadata=target_metadata,
                  compare_type=True, compare_server_default=True)
```

- `compare_type` and `compare_server_default` are **off by default** and both
  matter — without them autogenerate silently misses a changed default or a
  widened column.
- ~~Add `from sqlalchemy.dialects import postgresql` to `script.py.mako`~~ —
  tried and reverted. Alembic 1.20 adds that import itself when the diff needs
  it, so the template edit produced a *duplicate* import in the first
  revision. `script.py.mako` is stock.
- Autogenerate **does not detect `Computed` changes**. If the 0.5 threshold ever
  moves, hand-write drop+add. It's a product decision, not a schema one, so this
  is fine — but it's a real footgun.
- Partial/expression indexes are emitted correctly on create, but autogenerate
  compares them unreliably and sometimes proposes spurious drop/create pairs.
  Review every revision; with two tables that's a 30-second read.
- `Base.metadata.create_all()` never runs outside tests, or models and migration
  history drift — exactly the "unenforced contract" `initial_task.md` warns about.

**Enums: plain text columns. No native PG enum, no CHECK on label values.**

Five columns look enum-shaped: `source`, `overall_fit_label`, `top_gap`,
`best_angle` and `llm_calls.status`. The first four are argued below; `status`
is plain text for the dull reason that it has two values and validating it buys
nothing.

Native enums are out because `ALTER TYPE ... ADD VALUE` fights transactional
migrations, you can't remove a value without recreating the type and rewriting
every dependent column, and `top_gap`'s criteria are explicitly expected to
change.

A CHECK constraint is out for a stronger reason: **the DB has no business
rejecting what Jev actually returned.** An unexpected label should be stored and
visible, not a failed run and a lost paid call — a constraint converts a
data-quality signal into an outage. It's also the wrong constraint: the valid
label set is a function of `questions_hash`, not of the table, so a row written
under an older question set legitimately holds a label that is no longer valid.
And a DB enum on `source` would mean a migration per source, contradicting
"adding a source must be a new module plus a config entry."

Validation lives in Python, where the question definitions already are — the
SDK's typed answers give it for free at write time.

---

## 11. The HTTP API

FastAPI over the same `Session`, importing `jobmatch.models`. Five routes, all
of them thin: every one is a `repository` call plus a response model (the
cover note adds one `claude -p` call).

| Route | Purpose |
|---|---|
| `GET /api/stats` | headline figures, the fit distribution, the per-pitch breakdown — for one CV (`?cv=` by file name, else `stats_cv_hash`) and narrowed by `?country=` (repeatable), except the country breakdown itself, which ignores the country filter so it keeps its options |
| `GET /api/vacancies` | the ranked list — the §3.4 query, filters applied |
| `GET /api/vacancies/{id}` | one vacancy, plus the whole `answers` blob |
| `PATCH /api/vacancies/{id}/triage` | set or clear `seen_at` / `starred_at` / `hidden_at` |
| `POST /api/vacancies/{id}/cover-note` | a draft cover letter from `claude -p`, in the posting's language; never stored (`jobmatch/cover_note.py`) |

**Response models are Pydantic, and they are not a second schema.** This section's
rejection of "pydantic domain models mirroring the ORM models" stands: these
are *projections shaped for one screen*, built with `from_attributes`, and they
deliberately omit most columns (`raw`, every hash, `fetch_error`). A response
model that listed every column would be the duplicate declaration that rule
forbids.

Three decisions worth writing down, because each one is a place the UI could
lie if the API let it:

**The list LEFT JOINs.** A fetched-but-unmatched vacancy is a first-class state
(§3.1), so `match` is nullable in the response and the client renders it as
"not matched yet" rather than as a score of zero.

**`fit_probabilities` ships with every row.** Five floats projected out of
`answers`, so the list can draw the distribution instead of asserting a single
number. Per §5 it stays in JSONB — read on every page load, but never sorted or
filtered on.

**Confidence ships with every promoted answer.** `top_gap` alone is not a fact:
in the live corpus the top-scoring vacancy holds `top_gap = 'none'` at
confidence .17, with the four choices near-tied behind it. The API returns the
confidence and the probabilities so the client can refuse to print a winner it
should not trust; an API that returned only the winner would make that
impossible.

Pagination is keyset on `(overall_fit_score DESC, id)` to match
`ix_match_results_current_rank`, not `OFFSET` — though at this corpus size
either would be instant, and the reason to prefer it is that the cursor stays
correct when a run inserts rows mid-scroll.

No auth, no CORS in production: single user, and Vite proxies `/api` in dev.

---

