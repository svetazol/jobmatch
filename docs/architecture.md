# Architecture — vacancy pipeline + CV matching

Design for `initial_task.md`. Two decisions carry most of the weight:

1. **A source owns everything about its site.** Outside `sources/`, only two
   dataclasses exist — `Listing` and `VacancyData`. Adding a source is a new
   module plus a config entry.
2. **Idempotency is a fingerprint of the request, not a flag on a row.** Two
   unique constraints — one on the vacancy's natural key, one on the match
   request hash — are the whole of "safe and cheap to re-run".

Everything else follows from those.

**Settled:** Postgres runs in this repo's own Docker container
(`docker-compose.yml`, Postgres 18 on host port **5433** — 5432 belongs to an
unrelated project). The model is pinned
to an exact version, never `jev-latest` (§4). The package is `jobmatch/`;
`cv_match/` is absorbed into it (§8).

---

## 0. Verified against the live site — 2026-09-23

Probed before designing further, because two assumptions were load-bearing.

**The RSS feed returns the latest 20 items and does not paginate.** `page`,
`per_page` and `items_on_page` are all ignored — every variant returns 20.
Repeated calls return slightly different sets as new vacancies arrive, so the
feed is a *rolling window*, not a corpus.

This is the one finding that changes the design, and the answer is to lean on
idempotency rather than to fight it: run the pipeline on a schedule and let the
corpus accumulate. `ON CONFLICT (source, external_id)` means a vacancy seen in
ten consecutive runs is one row, and the match is paid for once. Hourly for a
day gets you far more than 20 vacancies, with no paging code and nothing to get
wrong. If a bulk backfill is ever genuinely needed, hh.ru's **HTML** search
results page *is* paginated — that would be a second function inside
`sources/hh.py` and nothing outside it would change.

**The prototype parser still works.** All `data-qa` selectors resolve on a live
page: title, company, salary, experience, description, and 13 `skills-element`
entries. Two details the prototype glosses over:

- The JSON-LD `JobPosting` block is present *alongside* the normal layout, not
  only as a branded-page fallback. It carries `datePosted`, `validThrough` and
  `identifier` — so read `published_at` from there rather than parsing the RSS
  `pubDate` string, and keep the whole block in `vacancies.raw`.
- Scraped text needs normalising: salary comes back as
  `'до\n5\xa0500\n$\nза\xa0месяц\nдо вычета налогов'`. Collapse non-breaking
  spaces and newlines in the source module, before it ever reaches the DB.
- `skills` mixes in language requirements (`'Английский — B1 — Средний'`)
  alongside real tech skills. Don't try to separate them; Jev reads them fine
  as-is.

**No rate limiting observed** at six requests over two seconds, no `Retry-After`
header, ~450ms per response. That is browsing-scale volume and proves very
little about sustained scraping — keep the politeness delay, and treat a 429 as
expected rather than exceptional. Vacancy pages are ~700KB each, which is the
real argument for the delay.

---

## 1. Layout

```
cv/
├─ pyproject.toml         # uv; [project.scripts] jobmatch = "jobmatch.cli:main"
├─ alembic.ini            # points at migrations/; DB URL comes from env, not from here
├─ config.toml            # checked in: enabled sources + their opaque params, knobs
├─ docker-compose.yml     # this project's Postgres 18, host port 5433
├─ .env                   # secrets only: DATABASE_URL, OPENROUTER_API_KEY
├─ .env.example           # checked-in template for .env
├─ migrations/versions/   # alembic; env.py imports jobmatch.models.Base.metadata
├─ data/cv.md             # private master CV (gitignored)
├─ docs/
├─ jobmatch/
│  ├─ config.py           # config.toml + .env -> frozen Settings dataclass
│  ├─ db.py               # engine + sessionmaker; the only reader of DATABASE_URL
│  ├─ models.py           # SQLAlchemy 2.0 declarative — THE schema source of truth
│  ├─ repository.py       # ~6 query/upsert functions; the only module writing SQL
│  ├─ pipeline.py         # run(): discover -> fetch -> persist -> match -> persist
│  ├─ cli.py              # argparse: run / discover / fetch / match / stats
│  ├─ sources/
│  │  ├─ __init__.py      # Listing, VacancyData, Source, SOURCES dict
│  │  └─ hh.py            # hh.ru: RSS discovery + page parsing (data-qa + JSON-LD)
│  ├─ matching/
│  │  ├─ __init__.py      # open_client, load_cv, inputs_fingerprint, match_vacancy
│  │  ├─ questions.py     # moved from cv_match/questions.py, unchanged
│  │  ├─ sanitize.py      # moved from cv_match/sanitize.py (SystemExit -> ValueError)
│  │  └─ jev.py           # TypeSafeClient call, vacancy->prompt, answer flattening
│  └─ api/                # PHASE 2 — slot fixed now, empty until then
│     ├─ app.py           #   FastAPI; imports jobmatch.models + jobmatch.repository
│     ├─ deps.py          #   get_session() yielding from jobmatch.db
│     └─ routers/{vacancies,triage}.py
├─ frontend/              # PHASE 2 — Vue 3 + Vite, separate npm project, no Python coupling
└─ tests/
   ├─ test_hh_parse.py    # saved HTML/RSS fixtures -> VacancyData, no network
   └─ test_pipeline.py    # fake Source + fake matcher, real DB session
```

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

```python
# jobmatch/sources/__init__.py
@dataclass(frozen=True, slots=True)
class Listing:
    external_id: str          # stable within the source; the source decides how
    url: str                  # canonical link, also what fetch() will hit
    title: str | None = None
    published_at: datetime | None = None

@dataclass(frozen=True, slots=True)
class VacancyData:
    external_id: str
    url: str
    title: str
    description: str          # plain text, already de-HTML'd by the source
    company: str | None = None
    salary: str | None = None
    experience: str | None = None
    skills: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)   # -> vacancies.raw JSONB

@dataclass(frozen=True, slots=True)
class Source:
    name: str
    discover: Callable[[Mapping[str, Any]], Iterable[Listing]]
    fetch: Callable[[Listing], VacancyData]

from . import hh
SOURCES: dict[str, Source] = {s.name: s for s in (hh.SOURCE,)}
```

```python
# jobmatch/sources/hh.py — everything hh-shaped is in here
RSS_URL = "https://hh.ru/search/vacancy/rss"
HEADERS = {"User-Agent": "...", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}

def discover(params):
    resp = requests.get(RSS_URL, params=dict(params), headers=HEADERS, timeout=20)
    resp.raise_for_status()
    for item in BeautifulSoup(resp.text, "xml").find_all("item"):
        url = _canonical(item.link.text)          # strip tracking query args
        yield Listing(external_id=_vacancy_id(url), url=url, title=item.title.text)

def fetch(listing) -> VacancyData: ...            # the initial_task.md prototype, minus file writing

SOURCE = Source(name="hh.ru", discover=discover, fetch=fetch)
```

`discover` yields whatever the feed currently holds — 20 items, per §0. The
pipeline neither knows nor cares that this is a rolling window; that fact is
absorbed entirely by the upsert.

Params go from config to the source **verbatim** and are never inspected in
between. hh.ru's repeated keys (`search_field=name&search_field=description`)
are a TOML list that `requests` expands; a JSON-API source would read the same
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

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| `id` | `bigint` identity | no | identity | Surrogate PK, FK target. |
| `source` | `text` | no | — | Source module key (`hh.ru`). Half the natural key. |
| `external_id` | `text` | no | — | Source's own id. Text, not bigint — the next source may use a slug. |
| `url` | `text` | no | — | Canonical URL, tracking params stripped. |
| `title` | `text` | yes | — | Null between discovery and fetch. |
| `company` | `text` | yes | — | Display only. |
| `salary_raw` | `text` | yes | — | As shown ("от 200 000 ₽"). Unparsed; nothing filters numerically. |
| `experience_raw` | `text` | yes | — | As shown. Same reasoning. |
| `description` | `text` | yes | — | Full plain text. Null until fetched; this is what makes a vacancy matchable. |
| `skills` | `text[]` | no | `'{}'` | Rendered as chips, never joined — an array, not a join table. |
| `published_at` | `timestamptz` | yes | — | From the source (RSS `pubDate`) when given. Secondary sort. |
| `raw` | `jsonb` | no | `'{}'` | Source-specific payload. The escape hatch that keeps the core columns source-agnostic. |
| `content_hash` | `char(64)` | yes | — | sha256 of the exact `job_description` string sent to Jev. Change detector. |
| `first_seen_at` | `timestamptz` | no | `now()` | First discovery hit. |
| `last_seen_at` | `timestamptz` | no | `now()` | Last discovery hit. |
| `fetched_at` | `timestamptz` | yes | — | Last successful detail parse. `IS NULL` = the fetch queue. |
| `fetch_attempts` | `smallint` | no | `0` | Consecutive failures; caps retries on permanently broken pages. |
| `fetch_error` | `text` | yes | — | Last failure message; NULL on success. |
| `delisted_at` | `timestamptz` | yes | — | Set only by a positive signal (404/410/archived page). |
| `seen_at` | `timestamptz` | yes | — | Triage. `IS NULL` powers "unseen only". |
| `starred_at` | `timestamptz` | yes | — | Triage. |
| `hidden_at` | `timestamptz` | yes | — | Triage. |
| `created_at` / `updated_at` | `timestamptz` | no | `now()` | Row insert / last write (`onupdate`). |

Triage is three nullable timestamps rather than booleans: same filter cost
(`seen_at IS NULL`), "when" for free, one consistent style, and no left join on
the hottest query. `fetch_attempts`/`fetch_error` earn their place because
without them every run re-fetches the same broken page forever — which is the
"cheap to re-run" requirement, not a hypothetical.

### 3.2 `match_results`

One row per *paid* Jev call. Append-only; the only update is setting
`superseded_at`.

| Column | Type | Null | Purpose |
|---|---|---|---|
| `id` | `bigint` identity | no | PK. |
| `vacancy_id` | `bigint` FK → `vacancies.id` ON DELETE CASCADE | no | Owner. |
| `llm_call_id` | `bigint` FK → `llm_calls.id` | no | The call that produced this row. Tokens, cost and provider live there — see §3.3. |
| `inputs_fingerprint` | `char(64)` | no | sha256 of the canonicalised request. **The idempotency key.** |
| `vacancy_content_hash` | `char(64)` | no | Copy at call time. Diagnostic: "the posting was edited". |
| `cv_hash` | `char(64)` | no | sha256 of the **sanitized** CV. Diagnostic: "I rewrote my CV". |
| `questions_hash` | `char(64)` | no | sha256 of canonical `QUESTIONS`. Diagnostic + label-vocabulary marker. |
| `is_qualified_noul` | `double precision` | no | Raw Noul 0–1. |
| `is_qualified` | `boolean` GENERATED ALWAYS AS (`is_qualified_noul >= 0.5`) STORED | no | Derived in the DB so the threshold can't drift between pipeline and API. |
| `overall_fit_score` | `double precision` | no | Score 0–1. **The sort key.** |
| `overall_fit_label` | `text` | no | Resolved from the response `legend`. Display + equality filter. |
| `overall_fit_confidence` | `double precision` | no | 0–1. |
| `top_gap` / `top_gap_confidence` | `text` / `double precision` | no | Winning Choice label + confidence. |
| `answers` | `jsonb` | no | Complete `answers` object incl. probabilities and legend. Source of truth; the promoted columns are projections. |
| `created_at` | `timestamptz` | no | Call time. |
| `superseded_at` | `timestamptz` | yes | NULL = current. |

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

| Column | Type | Null | Purpose |
|---|---|---|---|
| `id` | `bigint` identity | no | PK. |
| `vacancy_id` | `bigint` FK → `vacancies.id` ON DELETE **SET NULL** | yes | What the call was about. Nulled rather than cascaded, so deleting a vacancy never erases what you spent. |
| `inputs_fingerprint` | `char(64)` | no | The request hash — recorded even on failure, so you can see *which* question kept failing. |
| `model_requested` | `text` | no | What we asked for. Part of the fingerprint. |
| `model_resolved` | `text` | yes | What actually answered (`typesafe/jev-1.13`). Null on failure. **Not** in the fingerprint — unknowable before paying. |
| `provider` | `text` | yes | From the response. |
| `response_id` | `text` | yes | Jev/OpenRouter id. Support trail when you need to ask why an answer looks wrong. |
| `status` | `text` | no | `ok` or `error`. Plain text, not an enum — see §11. |
| `http_status` | `smallint` | yes | Transport status when there was one. Distinguishes 429 (back off) from 401 (fix the key) from a timeout (null). |
| `error_type` | `text` | yes | Exception class name. Groupable: `SELECT error_type, count(*) ... GROUP BY 1`. |
| `error_message` | `text` | yes | The detail, for reading. |
| `input_tokens` | `integer` | yes | From `usage`. |
| `output_tokens` | `integer` | yes | From `usage`. |
| `cost_usd` | `numeric(12,6)` | yes | From `usage.cost`. `numeric`, never float — money doesn't round in binary. |
| `duration_ms` | `integer` | yes | Wall clock. Catches "the API got slow" before it becomes "the API timed out". |
| `started_at` | `timestamptz` | no | Call start. |
| `finished_at` | `timestamptz` | yes | Call end; null if the process died mid-call. |

The questions this answers, each a one-liner:

```sql
-- what have I spent, ever / this month
SELECT sum(cost_usd) FROM llm_calls;
SELECT date_trunc('day', started_at) AS day, count(*), sum(cost_usd)
FROM llm_calls GROUP BY 1 ORDER BY 1 DESC;

-- what am I paying per useful answer, including the failures
SELECT status, count(*), sum(cost_usd), avg(duration_ms) FROM llm_calls GROUP BY 1;

-- what is actually breaking
SELECT error_type, http_status, count(*), max(started_at)
FROM llm_calls WHERE status = 'error' GROUP BY 1, 2 ORDER BY 3 DESC;

-- money spent on calls that never produced a stored result
SELECT sum(c.cost_usd) FROM llm_calls c
LEFT JOIN match_results m ON m.llm_call_id = c.id
WHERE m.id IS NULL AND c.cost_usd IS NOT NULL;
```

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

```sql
-- natural key: no duplicate vacancies, ever, enforced by Postgres
ALTER TABLE vacancies ADD CONSTRAINT uq_vacancies_source_external_id
  UNIQUE (source, external_id);

-- never pay twice
ALTER TABLE match_results ADD CONSTRAINT uq_match_results_vacancy_fingerprint
  UNIQUE (vacancy_id, inputs_fingerprint);

-- "the current match" is a DB-enforced fact, not a MAX(created_at) convention
CREATE UNIQUE INDEX uq_match_results_current
  ON match_results (vacancy_id) WHERE superseded_at IS NULL;

-- phase-2 support
CREATE INDEX ix_match_results_current_rank
  ON match_results (overall_fit_score DESC, vacancy_id) WHERE superseded_at IS NULL;
CREATE INDEX ix_vacancies_list ON vacancies (source, seen_at, last_seen_at DESC)
  WHERE hidden_at IS NULL AND fetched_at IS NOT NULL;
CREATE INDEX ix_vacancies_fetch_queue ON vacancies (first_seen_at)
  WHERE fetched_at IS NULL AND delisted_at IS NULL AND fetch_attempts < 3;

-- the call log is written far more often than it's read; one index, for
-- "what broke recently" and the per-day cost rollup
CREATE INDEX ix_llm_calls_started_at ON llm_calls (started_at DESC);
CREATE INDEX ix_llm_calls_errors ON llm_calls (started_at DESC)
  WHERE status = 'error';
```

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

```sql
INSERT INTO vacancies (source, external_id, url, title, published_at, raw, last_seen_at)
VALUES (...)
ON CONFLICT (source, external_id) DO UPDATE
SET last_seen_at = EXCLUDED.last_seen_at,
    url          = EXCLUDED.url,
    title        = COALESCE(EXCLUDED.title, vacancies.title),
    raw          = vacancies.raw || EXCLUDED.raw,
    updated_at   = now();
```

Canonical phase-2 list query:

```sql
SELECT v.id, v.title, v.company, v.url, v.source, v.seen_at, v.starred_at,
       m.overall_fit_label, m.overall_fit_score, m.is_qualified, m.top_gap
FROM vacancies v
JOIN match_results m ON m.vacancy_id = v.id AND m.superseded_at IS NULL
WHERE v.hidden_at IS NULL
  AND (:source IS NULL OR v.source = :source)
  AND (:min_fit IS NULL OR m.overall_fit_score >= :min_fit)
  AND (NOT :unseen_only OR v.seen_at IS NULL)
ORDER BY m.overall_fit_score DESC, v.published_at DESC NULLS LAST
LIMIT 50 OFFSET :offset;
```

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

Hit on `(vacancy_id, inputs_fingerprint)` → skip, zero cost. Miss → call,
supersede the current row, insert. The unique constraint keeps this correct
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

Typed columns for the three promoted answers, `answers` JSONB for the complete
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

**Sorting is correct for free.** Jev's `Score` already returns a number 0–1 —
position on the ordered scale — so `ORDER BY overall_fit_score DESC` is an
indexed float sort. The classic enum trap (`excellent < good < poor < strong <
weak` alphabetically) never arises because the label is never the sort key.
Worth a comment in the model and a test asserting the list endpoint orders by
score. `min_fit` is a float comparison; the API maps a UI label to a threshold
in one place, ideally derived from the stored `legend` rather than hardcoded.

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

### Idempotency per stage

| Stage | Guard | Cost of a no-op re-run |
|---|---|---|
| discover | none — always hits the source | 1 HTTP request per source |
| persist listing | `ON CONFLICT (source, external_id) DO UPDATE` | 1 upsert per listing |
| fetch | skipped when `fetched_at IS NOT NULL` | zero HTTP |
| match | skipped when `(vacancy_id, inputs_fingerprint)` exists | zero API spend |

A second `jobmatch run` five minutes later costs one RSS request and a handful
of upserts. "Safe and cheap" is discharged by two unique constraints.

### The loop

```python
# jobmatch/pipeline.py
def run(settings: Settings) -> RunReport:
    report = RunReport()
    with matching.open_client(settings) as client:
        cv = matching.load_cv(settings.cv_path)       # loaded + sanitized once per run
        for entry in settings.sources:
            source = SOURCES[entry.name]
            for listing in _safe_discover(source, entry.params, report):
                _process_one(source, listing, client, cv, settings, report)
    return report


def _process_one(source, listing, client, cv, settings, report) -> None:
    try:
        with Session(engine) as session, session.begin():   # one transaction per vacancy
            vacancy = repository.upsert_listing(session, source.name, listing)
            if vacancy.fetched_at is None:
                repository.apply_fetched(session, vacancy, source.fetch(listing))
                time.sleep(settings.fetch_delay)
            fp = matching.inputs_fingerprint(cv, _job_text(vacancy),
                                             QUESTIONS, settings.model)
            if not repository.has_match(session, vacancy.id, fp):
                outcome = matching.match_vacancy(client, cv, vacancy, settings.model)
                repository.save_match(session, vacancy.id, fp, outcome)  # supersedes + inserts
                report.matched += 1
                report.cost += outcome.call.cost_usd or 0
    except Exception as exc:                                # one bad vacancy != a dead run
        report.failures.append((listing.url, exc))
        log.warning("skipping %s: %s", listing.url, exc)
```

`match_vacancy` writes the `llm_calls` row itself, in its own short-lived
session, and returns it on the outcome. That is deliberate: the call log must
survive the per-vacancy rollback above. If a Jev call fails, or succeeds and
then the answer won't parse, the surrounding transaction rolls back the vacancy
work — but the row saying "this was attempted, here is what it cost and how it
failed" is already committed. A log that disappears when things go wrong is
worse than no log.

Failure isolation is structural, not defensive: **the transaction boundary is
one vacancy.** A parse failure, a 403 or an API error rolls back that vacancy
alone; everything committed before it stays committed, and the next run resumes
exactly where it stopped because every guard is state-based, not
position-based. `_safe_discover` wraps the generator so a source being down
costs one warning, not the other sources' work.

No retry library in phase 1: a transient failure is retried by the next run,
for free, because the row exists with `fetched_at IS NULL`.

---

## 8. Matcher integration

Importable as a plain function; no subprocess anywhere. `cv_match/` is absorbed
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
def open_client(settings) -> AbstractContextManager[TypeSafeClient]   # one client per run
def load_cv(path: Path) -> str                                        # read + sanitize_cv
def inputs_fingerprint(cv, job_text, questions, model) -> str
def match_vacancy(client, cv: str, vacancy: Vacancy, model: str) -> MatchOutcome
```

`MatchOutcome` is a small frozen dataclass holding the flattened fields, the
`raw: dict`, and the committed `LlmCall` row (`outcome.call`). It is the only place that knows the SDK's answer shape
(`answers.is_qualified.noul`, `answers.overall_fit.score` + `legend`,
`answers.top_gap.choice`, `usage.cost`) — that containment is what lets
`repository.save_match` stay a dumb column write.

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

```toml
[matching]
cv_path = "data/cv.md"
model   = "jev-1.13"        # pinned, never jev-latest — see §4

[fetch]
timeout_seconds = 20
delay_seconds   = 1.0

[[sources]]
name = "hh.ru"
params = { text = "Python", area = 28, search_field = ["name", "description"],
           work_format = "REMOTE" }
```

`params` is a free-form table copied into `SourceEntry.params` without being
looked inside. Validation is two checks in `load_settings()`: the file parses,
and every `name` is in `SOURCES` (fail fast: `unknown source 'lever'; known: hh.ru`).
One filter per source, as decided; the list-of-tables form already allows the
same source twice at zero cost today.

Alembic reads `DATABASE_URL` from the environment in `migrations/env.py`, not
from `alembic.ini`, so pipeline, API and migrations share one connection string.

---

## 10. SQLAlchemy models (sketch)

`jobmatch/models.py` is the single source of truth; Alembic follows it.

```python
NAMING_CONVENTION = {   # deterministic names so autogenerate is stable
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # set once here rather than DateTime(timezone=True) on twelve columns
    type_annotation_map = {dt.datetime: DateTime(timezone=True)}


class Vacancy(Base):
    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # natural key & location
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)

    # scraped core (null until the detail fetch succeeds)
    title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    salary_raw: Mapped[str | None] = mapped_column(Text)
    experience_raw: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    published_at: Mapped[dt.datetime | None] = mapped_column()

    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    content_hash: Mapped[str | None] = mapped_column(String(64))

    # lifecycle
    first_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    fetched_at: Mapped[dt.datetime | None] = mapped_column()
    fetch_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    fetch_error: Mapped[str | None] = mapped_column(Text)
    delisted_at: Mapped[dt.datetime | None] = mapped_column()

    # triage (single user: columns, not a table)
    seen_at: Mapped[dt.datetime | None] = mapped_column()
    starred_at: Mapped[dt.datetime | None] = mapped_column()
    hidden_at: Mapped[dt.datetime | None] = mapped_column()

    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(),
                                                    onupdate=func.now())

    matches: Mapped[list[MatchResult]] = relationship(
        back_populates="vacancy", cascade="all, delete-orphan",
        order_by="MatchResult.created_at.desc()",
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_vacancies_source_external_id"),
        Index("ix_vacancies_url", "url"),
        Index("ix_vacancies_list", "source", "seen_at", text("last_seen_at DESC"),
              postgresql_where=text("hidden_at IS NULL AND fetched_at IS NOT NULL")),
        Index("ix_vacancies_fetch_queue", "first_seen_at",
              postgresql_where=text(
                  "fetched_at IS NULL AND delisted_at IS NULL AND fetch_attempts < 3")),
    )


class MatchResult(Base):
    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True)
    llm_call_id: Mapped[int] = mapped_column(ForeignKey("llm_calls.id"))

    # idempotency
    inputs_fingerprint: Mapped[str] = mapped_column(String(64))
    vacancy_content_hash: Mapped[str] = mapped_column(String(64))
    cv_hash: Mapped[str] = mapped_column(String(64))
    questions_hash: Mapped[str] = mapped_column(String(64))

    # promoted answers — only what the UI sorts, filters or lists by
    is_qualified_noul: Mapped[float] = mapped_column(Double)
    is_qualified: Mapped[bool] = mapped_column(
        Boolean, Computed("is_qualified_noul >= 0.5", persisted=True))   # read-only in Python
    overall_fit_score: Mapped[float] = mapped_column(Double)             # THE sort key
    overall_fit_label: Mapped[str] = mapped_column(String(32))           # display only, never ORDER BY
    overall_fit_confidence: Mapped[float] = mapped_column(Double)
    top_gap: Mapped[str] = mapped_column(String(64))
    top_gap_confidence: Mapped[float] = mapped_column(Double)

    answers: Mapped[dict[str, Any]] = mapped_column(JSONB)   # complete record

    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    superseded_at: Mapped[dt.datetime | None] = mapped_column()

    vacancy: Mapped[Vacancy] = relationship(back_populates="matches")
    call: Mapped[LlmCall] = relationship()

    __table_args__ = (
        UniqueConstraint("vacancy_id", "inputs_fingerprint",
                         name="uq_match_results_vacancy_fingerprint"),
        Index("uq_match_results_current", "vacancy_id", unique=True,
              postgresql_where=text("superseded_at IS NULL")),
        Index("ix_match_results_current_rank", text("overall_fit_score DESC"), "vacancy_id",
              postgresql_where=text("superseded_at IS NULL")),
        CheckConstraint(
            "overall_fit_score BETWEEN 0 AND 1 AND is_qualified_noul BETWEEN 0 AND 1 "
            "AND overall_fit_confidence BETWEEN 0 AND 1 AND top_gap_confidence BETWEEN 0 AND 1",
            name="probabilities_in_range"),
    )
```

```python
class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # SET NULL, not CASCADE: deleting a vacancy must not erase what it cost
    vacancy_id: Mapped[int | None] = mapped_column(
        ForeignKey("vacancies.id", ondelete="SET NULL"), index=True)

    inputs_fingerprint: Mapped[str] = mapped_column(String(64))
    model_requested: Mapped[str] = mapped_column(String(64))
    model_resolved: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(64))
    response_id: Mapped[str | None] = mapped_column(String(128))

    # outcome
    status: Mapped[str] = mapped_column(String(16))          # "ok" | "error"
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    # cost & timing
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))   # never Float
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_llm_calls_started_at", text("started_at DESC")),
        Index("ix_llm_calls_errors", text("started_at DESC"),
              postgresql_where=text("status = 'error'")),
    )
```

The one range `CheckConstraint` is worth having: it's the only thing that
catches an SDK shape change writing garbage into the sort key.

`cost_usd` is `Numeric`, never `Double`: summing float money accumulates error,
and this column exists to be summed.

---

## 11. Alembic and enums

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
- Add `from sqlalchemy.dialects import postgresql` to `script.py.mako`, or the
  first autogenerated revision fails at import on `postgresql.JSONB()`.
- Autogenerate **does not detect `Computed` changes**. If the 0.5 threshold ever
  moves, hand-write drop+add. It's a product decision, not a schema one, so this
  is fine — but it's a real footgun.
- Partial/expression indexes are emitted correctly on create, but autogenerate
  compares them unreliably and sometimes proposes spurious drop/create pairs.
  Review every revision; with two tables that's a 30-second read.
- `Base.metadata.create_all()` never runs outside tests, or models and migration
  history drift — exactly the "unenforced contract" `initial_task.md` warns about.

**Enums: plain text columns. No native PG enum, no CHECK on label values.**

Four columns look enum-shaped: `source`, `overall_fit_label`, `top_gap`, and
`llm_calls.status`. The first three are argued below; `status` is plain text
for the dull reason that it has two values and validating it buys nothing.

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

## 12. Considered and rejected

| Rejected | Why |
|---|---|
| `SourcePlugin` ABC + `register_source()` + entry-point discovery | Solves third-party plugin distribution. One developer, sources in this repo: a dict literal and an import line do the job and read in five seconds. |
| Repository/Unit-of-Work pattern, generic `Storage` interface | SQLAlchemy's `Session` *is* the unit of work. `repository.py` is a flat module of ~6 named functions to keep SQL out of `pipeline.py` — not a layer, never a class hierarchy. |
| DI container / `Pipeline` class with injected collaborators | `run(settings)` takes its dependencies as arguments; tests pass a fake `Source` and a fake client. Nothing to wire. |
| Celery / RQ / APScheduler | `cron` calls `jobmatch run`. Idempotency, not a broker, is what makes re-runs safe — and it's already required for correctness. |
| Pydantic domain models mirroring the ORM models | Two declarations of one concept. Phase 2 derives FastAPI response models from the ORM classes, for the fields the UI shows. |
| A `sources` table, a `crawl_log`/`search_runs` table, `skills`/`companies` tables | Sources are config. Nothing reads a crawl log. Skills are display strings; nothing joins on company. |
| Per-source tables or a `vacancy_hh` detail table | One table plus a JSONB `raw` column absorbs source-specific fields with no migration per source. |
| A separate `normalizer.py` / `parsers/` layer | Normalisation *is* the source's job — that's what `VacancyData` is for. A shared normaliser would grow hh-specific branches, which `initial_task.md` forbids. |
| Caching raw HTML to disk or a `raw_html` column | "No per-vacancy files, ever"; and megabyte blobs bloat the DB for a re-parse that a re-fetch already covers. |
| `Matcher` protocol / pluggable LLM providers | One matcher exists. `matching/jev.py` is already the containment point. |
| Soft-delete flag on vacancies | `hidden_at` and `delisted_at` mean different things and the UI needs both. |
| tenacity / backoff policy / dead-letter table | The next run retries everything unfinished for free; `fetch_attempts` caps the pathological case in two columns. `llm_calls` records failures but nothing branches on it. |
| A `runs` table grouping each invocation | `llm_calls.started_at` already groups by time, and `RunReport` prints the summary at the end of the run. Add a run id only when you actually want to compare two runs. |
| async / httpx / concurrent fetching | One user, one filter, tens of vacancies per run, plus a politeness delay. Sequential `requests` is simpler and kinder to the site. |
| `logging.config` dict / structured logging module | `logging.basicConfig()` in `cli.py`, `log = logging.getLogger(__name__)` elsewhere. |
| Named multi-search configs, per-source schedules, event hooks | Explicitly out of scope. |
| A second project or read-only DB user for the web app | `initial_task.md`: that makes the schema an unenforced contract between two codebases. |
