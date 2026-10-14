# jobmatch

Scrapes Python vacancies from hh.ru into Postgres and scores each one against a
CV with an LLM, shown in a Vue UI. On click it also asks a model who posted a
vacancy, who recruits there, and drafts a cover note.

## How the AI is used

| Use | Provider | Code |
|---|---|---|
| Match a vacancy to the CV | Jev (TypeSafe, via OpenRouter) | `jobmatch/matching/` |
| Who posted it, who recruits there | OpenRouter, strict JSON | `jobmatch/research/` |
| Cover note | `claude -p` | `jobmatch/cover_note.py` |

All three share `jobmatch/llm/`: one fingerprint rule, so the same request is
never paid for twice, and one ledger (`llm_calls`) that records every call.
Each provider has one module there. The full map, and how to add a new use, are
in `docs/architecture.md` §8.

## Run

```bash
docker compose up -d          # Postgres on :5433
cp .env.example .env          # DATABASE_URL, OPENROUTER_API_KEY
uv sync && uv run alembic upgrade head

uv run jobmatch run --no-match --limit 20   # discover and fetch; free
uv run jobmatch match --limit 5             # pays for each new match
uv run jobmatch trace --last 10             # what the models were asked and cost

uv run uvicorn jobmatch.api.app:app --reload   # API on :8000
cd frontend && npm install && npm run dev      # UI on :5173
```

Models and search settings live in `config.toml`. Tests need the database:
`uv run pytest`.

## Docs

`docs/architecture.md` (design), `docs/pipeline.md` (the run phases),
`docs/company-research.md` (the company questions), `docs/decisions.md` (what
was refused and why).
