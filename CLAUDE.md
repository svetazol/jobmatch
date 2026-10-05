# jobmatch

Scrapes Python vacancies from hh.ru into Postgres and matches each one against
a CV with Jev (TypeSafe AI, via OpenRouter), served to a Vue UI. Single user,
one machine, one database.

## Read before changing anything

The docs hold the reasoning the code cannot; read the relevant one first.

- `docs/architecture.md`: the seams, the idempotency rules, the schema's
  reasoning, the API's decisions.
- `docs/pipeline.md`: the three phases and what is on disk after each one.
- `docs/decisions.md`: what was considered and **refused**. Check it before
  proposing an abstraction, a library, or a config setting. A rejected option
  stays rejected unless the user reopens it.
- `docs/jev.md`: the matcher model and SDK.
- `docs/initial_task.md`: the original brief. Frozen, never edited to match the
  code.
- `jobmatch/models.py`: the schema's source of truth, with the reason for each
  column in comments.

## Commands

```bash
docker compose up -d                       # Postgres on host port 5433
cp .env.example .env                       # DATABASE_URL, OPENROUTER_API_KEY
uv sync
uv run alembic upgrade head

uv run pytest                              # needs the DB up and migrated
uv run pytest tests/test_docs.py           # doc accuracy; no DB needed

uv run jobmatch run [--only bg] [--no-match] [--limit N]
uv run jobmatch discover | fetch --pending | match [--dry-run] | cv-hash
uv run python -m jobmatch.sources.hh russia   # what a country expands to

uv run uvicorn jobmatch.api.app:app --reload  # API on :8000
cd frontend && npm run dev                    # UI on :5173, proxies /api
cd frontend && npm run typecheck
```

**`jobmatch match` and `jobmatch run` without `--no-match` spend real money.**
Don't run them unless asked. Use `match --dry-run --limit N` to preview (it
still pays, but writes nothing). `discover` and `fetch` are free but hit
hh.ru, so keep them small (`--limit`).

## Layout

```
jobmatch/
  sources/     the web: one module per site; hh/ is the only one
  matching/    the LLM: questions, CV sanitising, fingerprint; jev.py alone knows the SDK
  models.py    the schema (SQLAlchemy 2.0)
  repository.py  flat module of named queries, keeps SQL out of pipeline.py
  pipeline.py  the only module allowed to know all three areas above
  cli.py       thin caller; the one place asyncio.run happens
  api/         FastAPI, thin: each route is a repository call plus a response model
  config.py    loads and validates config.toml
  db.py        engine + SessionLocal; the only reader of DATABASE_URL
migrations/    Alembic (synchronous, reads DATABASE_URL from env)
frontend/      Vue 3 + Vite + PrimeVue; src/api/types.ts is the API contract
tests/         pytest, against the real database
data/          CVs (gitignored, no committed copy)
```

## Rules that are easy to break

- **The database is the only store.** No per-vacancy files, no HTML cache.
- **Never pay for the same match twice.** `inputs_fingerprint` hashes the whole
  Jev request: the sanitised CV, the vacancy's job text, the questions and the
  model. Anything that changes the CV or the vacancy must flow through it.
  Don't add a version constant.
- **The CV is sanitised before it is hashed or sent**, and loaded once per run.
  The CV the next run uses is `cv_path` in `config.toml`.
- **The model is pinned** (`jev-1.13`), never `jev-latest`: it is part of the
  fingerprint.
- **Every guard is a condition on the rows.** There is no queue table, run
  record or cursor. `fetched_at IS NULL` *is* the fetch queue.
- **A vacancy is keyed by `(source, external_id)`**, never by URL. Discovery
  only bumps `last_seen_at`; it never touches what the fetch phase wrote.
- **Nothing deletes vacancies.** Use `hidden_at`. Absence from search is not
  closure: only a positive 404/archived signal sets `delisted_at`.
- **Source-specific fields go in the `raw` JSONB column**, not new tables or
  columns, unless the UI filters on them (as with `country` and
  `work_formats`).
- **No PG enums.** A new source must not need a migration.
- **Config holds choices only.** Site-measured limits (`RATE`, `CRAWL`,
  `ORDER_BY`, ceiling) live in `jobmatch/sources/hh/__init__.py`. Search params
  pass to the source verbatim and are never inspected (the one exception is
  `stats.py::_search()`).
- **`MAX_FETCH_ATTEMPTS` is in a partial index.** Changing it needs a
  migration.
- **Pydantic in `api/schemas.py` holds per-screen projections**, not a mirror
  of the ORM.
- **Async throughout**, apart from Alembic. Library code never calls
  `asyncio.run`. CPU-heavy parsing goes through `asyncio.to_thread`.
  `expire_on_commit=False` is load-bearing.
- **Migrations:** autogenerate misses `Computed` changes and mishandles partial
  indexes, so review every revision. Never call `create_all` outside tests.
- **KISS:** no ABCs, registries, DI, plugin systems, Celery or tenacity. Each
  abstraction must serve a requirement that exists today.
- **Never edit `data/cv.md`.** `data/` is gitignored, so an edit there cannot
  be reverted.

## Tests

- Tests run against the real Postgres, deliberately: the behaviour under test
  (LEFT JOINs, partial indexes) belongs to the database. Each test file writes
  rows under its own `source` name and cleans them up afterwards.
- Async tests use `pytest.mark.anyio`, with one session-wide asyncio loop set
  in `tests/conftest.py`.
- Nothing in the test suite calls Jev for real.
- `tests/test_docs.py` checks every tracked `.md` file, this one included. A
  doc must not name anything listed in `GONE`, and every backticked
  `jobmatch/…py`, `docs/…md` or `config.toml` path must exist. When you remove
  or rename something, add it to `GONE` with its replacement.

## Style

- Python 3.14: `from __future__ import annotations`, frozen `slots`
  dataclasses, PEP 695 generics, modules of plain functions.
- Comments and docstrings explain *why*, often citing a measurement and its
  date. Keep that density, and don't restate in a doc a value that lives in
  code. Point at the code instead.
- When a design choice is rejected or reversed, record it in
  `docs/decisions.md`.
- Commit subjects are short imperative sentences that say what changed and
  why, e.g. "Let hh own hh, and the config own only choices".
