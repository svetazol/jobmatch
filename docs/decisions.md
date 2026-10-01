# Decisions

What was considered and refused, and why. Code shows what is; only this can say
what was looked at and turned down — so a rejected option stays rejected until
someone deliberately revisits it.

Entries are dated where the reason rests on a measurement. If you want to reopen
one, check the measurement still holds first.

---

## Architecture

| Rejected | Why |
|---|---|
| `SourcePlugin` ABC, `register_source()`, entry-point discovery | Solves third-party plugin distribution. One developer, sources in this repo: `SOURCES = {s.name: s for s in (hh.SOURCE,)}` reads in five seconds. |
| A source folder with a declarative `source.toml` manifest | Same, one layer down. Nothing loads sources at runtime. |
| Repository / Unit-of-Work pattern, a generic `Storage` interface | SQLAlchemy's `Session` *is* the unit of work. `repository.py` is a flat module of named functions to keep SQL out of `pipeline.py` — not a layer, never a class hierarchy. |
| DI container, a `Pipeline` class with injected collaborators | `run(settings)` takes its dependencies as arguments; tests pass a fake `Source` and a fake client. Nothing to wire. |
| Celery / RQ / APScheduler | `cron` calls `jobmatch run`. Idempotency, not a broker, is what makes re-runs safe — and it is already required for correctness. |
| A separate `normalizer.py` or `parsers/` layer | Normalisation *is* the source's job; that is what `VacancyData` is for. A shared normaliser grows site-specific branches. |
| `Matcher` protocol, pluggable LLM providers | One matcher exists. `matching/jev.py` is already the containment point. |
| `Source.describe()` returning a search description | Would amend the documented two-question contract and add a type, to move 28 lines out of one router. `stats.py::_search()` declares itself the sanctioned exception instead. |
| `logging.config` dict, structured logging | `basicConfig()` in `cli.py`, `getLogger(__name__)` elsewhere. |

## Storage

| Rejected | Why |
|---|---|
| Per-vacancy files, anywhere, for anything | The database is the only store. This is the one rule with no exceptions. |
| Caching raw HTML to disk or in a `raw_html` column | Megabyte blobs for a re-parse that a re-fetch already covers. |
| Per-source tables, or a `vacancy_hh` detail table | One table plus a JSONB `raw` column absorbs source-specific fields with no migration per source. |
| `sources`, `crawl_log`, `search_runs`, `skills`, `companies` tables | Sources are config. Nothing reads a crawl log. Skills are display strings; nothing joins on company. |
| A `runs` table grouping each invocation | `llm_calls.started_at` groups by time and `RunReport` prints the summary. Add a run id when you actually want to compare two runs. |
| Pydantic domain models mirroring the ORM models | Two declarations of one concept. `api/schemas.py` derives from the ORM classes. |
| A soft-delete flag on vacancies | `hidden_at` and `delisted_at` mean different things and the UI needs both. |
| A second project, or a read-only DB user, for the web app | Makes the schema an unenforced contract between two codebases. `api/` lives inside the package and imports `models.py`. |
| A stored `stale` flag | Derived at read time from `last_seen_at`. A stored flag needs a job to maintain it and is wrong between runs. |

## Retries and failure

| Rejected | Why |
|---|---|
| tenacity, a backoff policy, a dead-letter table | The next run retries everything unfinished for free; `fetch_attempts` caps the pathological case in two columns. `llm_calls` records failures but nothing branches on them. |
| `max_fetch_attempts` as a config setting | **Was one, and it could not turn.** The number is also in `fetch_queue`'s predicate and in `ix_vacancies_fetch_queue`, so raising it emptied the queue instead of retrying more. Now `models.MAX_FETCH_ATTEMPTS`; changing it needs a migration. |

## Crawling

| Rejected | Why |
|---|---|
| ~~Concurrent fetching~~ — **reversed** | Rejected at design time as unnecessary; adopted once the corpus reached thousands of pages. A thread pool, not asyncio: the work is `requests` + BeautifulSoup, both synchronous, and `Source.fetch` stays a plain callable. The rate is `workers / delay` per second. |
| Adaptive coverage: probe `totalResults`, split a query when it exceeds the cap | Appealing — it would delete the dated counts from the code. Blocked twice as of 2026-09-30: `areas.json` is flattened to country→regions, so a region has no children to recurse into and the only move left is the experience split that `coverage()` already states; and `totalResults` is discarded through four functions that all return `list[Listing]`, one of which (`_listings_from_anchors`, the documented fallback) has no total at all — so it would need the static table anyway. |
| A `--plan` flag printing the query tree | A second surface to inspect complexity we chose not to add. Reconsider only alongside adaptive coverage. |
| A configurable search URL | hh has one search endpoint, and the domain is not a filter — `headhunter.ge` without an `area` param returns Moscow. A URL in config would look like a way to scope a crawl by country while doing nothing of the kind. |
| Regenerating `areas.json` with full depth | Triples the file to enable a recursion the ceiling makes pointless: a single region holding more than 2 000 Python postings does not exist. |

## Config

| Rejected | Why |
|---|---|
| A second config file for a narrower sweep | `config.bg.toml` was `config.toml` with three lines changed, and it drifted — it ran the fetch rate the main file recorded as unsafe. Named sweeps in one file cannot drift from themselves. |
| YAML | The nesting advantage was real against six `[[sources]]` blocks; the current shape is shallow and labelled, which is TOML's strength. `tomllib` is stdlib, and unquoted YAML scalars are typed by guesswork — in the one list you edit most, a term like `no` would become a boolean. |
| Config as a Python module | Needs an importlib loader a reader must decode, and `exec_module` would run per `/api/stats` request behind an `except` that would have to widen to `except Exception` — at which point a `NameError` renders unfiltered market stats as if they were filtered. TOML cannot grow an `if os.environ.get(...)`. |
| A config file inside `sources/hh/` | Those values are constants that change in the same commit as the parser. A TOML file there buys packaging, a parse, and a missing-file path, and loses the adjacency that keeps `RATE` next to the measurement justifying it. |
| Search terms and sweeps in `sources/hh/` | The line is whether the site determines the value. hh's ceiling is 2 000 because hh 404s at page 40; hh has no opinion about `python`. Also: config in the source module makes a touched file stop distinguishing "the parser changed" from "I want Go jobs now". |
| `order_by` as a config setting | Under a per-query ceiling the ordering decides *which* 2 000 results you get, so it is coverage, not preference. Now `hh.ORDER_BY`; a configured value still wins. |
| Named multi-search configs, per-source schedules, event hooks | Out of scope. |

## Documentation

| Rejected | Why |
|---|---|
| A design doc that mirrors the code | `architecture.md` once held a models sketch and a verified-facts section; both became second sources of truth and drifted. Facts now live beside the values they justify; docs point at them. |
| A task-sequence doc | Was `tasks.md`, 22 completed items. That is `git log`. |
