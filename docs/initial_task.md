> **Historical.** The original brief, kept because the code cites it — the
> "no per-vacancy files" and "no second codebase reading this schema" rules
> come from here. Superseded wherever it conflicts with the code or with
> `docs/decisions.md`; not edited to match.

# Task: vacancy scraping + CV matching pipeline

## Goal

Pull job vacancies from job sites (starting with hh.ru), store them, run the
existing CV/job matcher against each, and store that result too. Later, browse
and triage the results through a web UI.

## Architecture principles

The architecture should be **good, but KISS** — clean seams and obvious
boundaries, without machinery this project doesn't need. One person, one
machine, one database. Concretely:

- Prefer plain functions and small modules over layers, factories and
  registries. No queue, no scheduler, no DI container, no microservices.
- Every abstraction has to earn its place against a requirement that already
  exists. "We might need it later" is not one.
- Re-running anything must be safe and cheap: no duplicate vacancies, and
  never pay for the same match twice.
- One source of truth per concept — especially the DB schema.

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.14, managed with uv |
| Database | PostgreSQL |
| DB access | SQLAlchemy 2.0 — models are the single source of truth for the schema; Alembic for migrations |
| Scraping | requests + beautifulsoup4 |
| Matching | Jev (TypeSafe AI) via OpenRouter — `typesafe_sdk`, see `jev.md` |
| Backend API | FastAPI |
| Frontend | Vue 3 + Vite |

## Requirements

### Phase 1 — the pipeline

1. **Discover** vacancies via a filtered search, not a fixed list. hh.ru
   exposes this as an RSS feed with query params (`text`, `area`,
   `work_format`, `search_field`, ...). Example filter URLs:
   - `https://hh.ru/search/vacancy/rss?text=python`
   - `https://hh.ru/search/vacancy/rss?hhtmFromLabel=header&hhtmFrom=vacancy&text=Python&area=28&search_field=name&search_field=description&work_format=REMOTE&enable_snippets=true`

2. **Fetch** the full page for each discovered vacancy (RSS entries don't
   carry enough detail to match on — no skills list, no full description).
   See the reference scraper below for the parsing approach.

3. **Persist vacancies to PostgreSQL.** No per-vacancy files — the reference
   scraper below writes a `.md` and a `.json` per vacancy; that pattern is
   explicitly *not* wanted here. Scraped data goes straight into the DB.

4. **Match** each stored vacancy against the CV using the existing matcher,
   and persist that result to the DB as well. The matcher must be callable
   as a plain function/import from the pipeline — going through a CLI
   subprocess is not required and should be avoided.

### Phase 2 — API and UI

Built later, but phase 1 must not make it awkward: the schema and the seams
should already suit a web app reading and writing the same database.

5. **Backend API** — FastAPI over the same database, importing the pipeline's
   SQLAlchemy models rather than re-declaring the schema. Read endpoints for
   the ranked vacancy list and for vacancy detail; write endpoints for triage
   (mark seen, star, hide).

6. **Frontend** — a Vue 3 + Vite SPA: vacancies ranked by match score with
   filters (source, minimum fit, unseen only), a detail view showing the
   description, skills and the matcher's answers, and the triage actions.

Single user throughout — no accounts, no roles, no multi-tenancy.

## Reference: existing hh.ru vacancy-page scraper (prototype)

Working prototype for step 2 — parses a single hh.ru vacancy page via
`data-qa` attributes with a JSON-LD fallback for branded layouts. Reuse the
parsing logic; drop the `to_markdown()` / per-file `.md`+`.json` writing at
the bottom (superseded by requirement 3).

```python
"""Fetch an hh.ru vacancy from the main site (no API) and save it as Markdown.

Usage:  python hh_vacancy.py https://hh.ru/vacancy/137615828 [out_dir]
Deps:   pip install requests beautifulsoup4
"""
import json
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def text_of(soup, qa):
    el = soup.find(attrs={"data-qa": qa})
    return el.get_text("\n", strip=True) if el else None


def from_json_ld(soup):
    """Fallback: hh.ru embeds a schema.org JobPosting block."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def fetch_vacancy(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    vac = {
        "url": url,
        "title": text_of(soup, "vacancy-title"),
        "company": text_of(soup, "vacancy-company-name"),
        "salary": text_of(soup, "vacancy-salary"),
        "experience": text_of(soup, "vacancy-experience"),
        "description": text_of(soup, "vacancy-description"),
        "skills": [
            s.get_text(strip=True)
            for s in soup.find_all(attrs={"data-qa": re.compile(r"^skills-element")})
        ],
    }

    # Branded vacancies sometimes use a different layout: fall back to JSON-LD.
    if not vac["description"]:
        ld = from_json_ld(soup)
        if ld:
            vac["title"] = vac["title"] or ld.get("title")
            org = ld.get("hiringOrganization") or {}
            vac["company"] = vac["company"] or org.get("name")
            desc_html = ld.get("description", "")
            vac["description"] = BeautifulSoup(desc_html, "html.parser").get_text(
                "\n", strip=True
            )

    if not vac["description"]:
        raise RuntimeError("Description not found; the page layout may have changed.")
    return vac


def to_markdown(v):
    lines = [f"# {v['title'] or 'Untitled vacancy'}", ""]
    for label, key in [("Company", "company"), ("Salary", "salary"),
                       ("Experience", "experience"), ("URL", "url")]:
        if v.get(key):
            lines.append(f"**{label}:** {v[key]}  ")
    lines += ["", "## Description", "", v["description"]]
    if v["skills"]:
        lines += ["", "## Key skills", ""] + [f"- {s}" for s in v["skills"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    url = 'https://hh.ru/vacancy/137615828'
    out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)

    vacancy = fetch_vacancy(url)
    vac_id = re.search(r"/vacancy/(\d+)", url).group(1)
    (out_dir / f"vacancy_{vac_id}.md").write_text(to_markdown(vacancy), encoding="utf-8")
    (out_dir / f"vacancy_{vac_id}.json").write_text(
        json.dumps(vacancy, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved vacancy {vac_id}: {vacancy['title']}")
```

## Decisions already made

- **Several job sources are coming** — hh.ru is only the first. Adding a source
  must be a new module plus a config entry, never a change to the pipeline.
  Nothing outside a source module may assume hh.ru's shape: the next source may
  be a JSON API or a paginated HTML search rather than an RSS feed, so a source
  is configured with opaque query params, not with a URL.
- One search filter per source is enough ("python" filters plenty) — no named
  multi-search configuration.
- `cv_match/*` (its layout, name, internals) is not fixed — it can be
  restructured as needed to fit the pipeline; it doesn't have to stay as-is.
- No per-vacancy files, ever. The database is the only store.
- The pipeline and the web app share one database and one repository; a second
  project reading this DB would make the schema an unenforced contract between
  two codebases.
