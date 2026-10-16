# Company research: who is behind a vacancy, and whom to write to

The goal: for a vacancy worth applying to, a real person to write to. The
project is also where the AI engineering stack is practised (`CLAUDE.md`), so
the AI parts are built as small, measured steps:

| | Step | AI | Status |
|---|---|---|---|
| 0 | **Company lookup** — the employer's hh page: name, the link it publishes, city, industries | none | built 2026-10-07 |
| 1 | **Employer identity** — who posted the vacancy (the agency itself, for an agency posting), its homepage and careers page, and, for an agency, the client it hires for if the posting names it | one structured-output call, no tools | the call built 2026-10-12; the card 2026-10-13; eval to come (§2) |
| 2 | **Recruiters on LinkedIn** — one click, one model call that searches the web for recruiters and HR at the company; only people whose profile the search cited are shown | one structured-output call with web search, through OpenRouter | the search and its table built 2026-10-13; the block to come (§3) |
| 3 | **An agent that picks its own searches** — only if step 2 shows misses on real clicks | a tool-using agent | not designed |

Each step is traced (every call's request and response in the database) and
scored on a small labelled eval set before it is trusted. A first attempt at
steps 2–3 as one free-roaming LangChain agent was built and dropped;
`docs/decisions.md` ("People search") has what it established; its code was
deleted on 2026-10-12, after the parts worth keeping moved into what is now `jobmatch/research/`.

---

## 1. Built: the company lookup

A **Look up** action in the vacancy page's Company section
(`frontend/src/components/CompanyPanel.vue`) calls
`POST /api/vacancies/{id}/company` (`jobmatch/api/routers/company.py`), which
runs `company_research.lookup_employer` (`jobmatch/company_research.py`):

1. The vacancy's `employer_external_id` — kept by every fetch from the
   posting's `/employer/<id>` link. A row fetched before that column existed
   gets it from one re-read of its page, written alone, so a click never
   changes `content_hash` (and never makes the next match pay).
2. `source.fetch_employer` reads `hh.ru/employer/<id>`
   (`jobmatch/sources/hh/__init__.py`, `parse_employer`).
3. `repository.upsert_employer` stores an `employers` row — only for companies
   someone asked about; the crawl never writes one.

What hh publishes is not always the homepage. A sample of 23 employers behind
random fetched vacancies (2026-10-12): 16 linked their homepage, 5 a careers
or HR site (yandex.ru/jobs, rabota.vtb.ru, job.alfabank.ru, rabotavdomru.ru,
hr.aviacenter.ru), 2 no site at all, and 1 was a recruiting agency posting for
an unnamed client. Three postings came from divisions ("BI.ZONE Направление
Кибербезопасность", "ООО HeadHunter::Analytics", "Банк ВТБ (ПАО),
Технологический блок"). Step 1 exists for these cases.

---

## 2. Step 1: employer identity (approved 2026-10-12)

Designed by the designer agent, structure reviewed by the architect agent,
approved by the user with the architect's changes folded in.

### What the user sees

As designed; since 2026-10-15 the identity runs only on demand, to fill a gap
(§3, What the user sees).

1. **Look up company** runs the hh lookup as today (~2 s), then a second
   request identifies the employer (~1–3 s, ~$0.0002).
2. The card adds: "Who it is: ЭР-Телеком · homepage ertelecom.ru ↗ · careers
   rabotavdomru.ru ↗". For an agency: "Recruiting agency — hiring for X", or
   "— client not named". A field the model did not know reads "unknown". A
   homepage that failed the reachability check is shown unlinked, "(did not
   answer)". A small line: model, cost, "call #1234".
3. If identification fails, the hh result stays and a warning appears under
   it. **Refresh** asks again only if the inputs changed; otherwise it is free
   and instant.
4. In the terminal: `jobmatch identify --vacancy N`, `jobmatch trace [ID]`
   (recent calls, or one in full), `jobmatch eval employers [--model M]`.

### Where the code lives

- `jobmatch/research/employer.py`: the question (`Poster` in, `Identity` out,
  `EMPLOYER_SYSTEM`) and `reachable`, the check on a URL it names (never its
  content: a Cyrillic name would fail a real Latin homepage).
- `jobmatch/llm/openrouter.py`: the call, its deadline and its ledger row.
- `jobmatch/company_research.py`: `poster_of` and `identify_employer`, the
  cache check, the call and the save.
- `jobmatch/repository.py`: `identity_by_fingerprint`, `latest_identity`,
  `save_identity`.
- `jobmatch/api/routers/company.py`: the route and its status codes.
- **evals.py** (not built yet): scores `research.ask_employer` against
  labelled cases, bypassing the cache.

### Data

- **`employer_identities`**: `id`, `vacancy_id` (FK), `inputs_fingerprint`
  (unique — the cache key), `answer` (JSONB, `Identity.model_dump()`),
  `homepage_ok` and `careers_ok` (did each URL answer; NULL without one, and
  only a URL that answered is linked), `llm_call_id` (FK), `created_at`.
  Append-only; the newest row for a vacancy is shown. **Per posting, not on
  `employers`**: the posting is part of the request (it is what reveals an
  agency and its client), so the answer belongs to the request, and storing it
  per company would need a rule for which posting's answer wins. Cost: one
  more ~$0.0002 call per further posting of the same company.
- **`llm_calls`** gains `kind` (`"match"` | `"employer"` | later `"recruiters"`,
  no default — a new caller must say what it is; the market view's spend sums
  `match` only), `request` and `response` (JSONB: what was sent and received —
  the trace). Jev rows leave `request`/`response` NULL; Jev's SDK does not
  expose a raw request worth keeping. The migration fills `kind` from the
  data: `match` where `model_requested LIKE 'jev-%'`, else `people` (the 5
  paid runs of the dropped attempt).
- **evals/employers.toml** (new, committed): one `[[case]]` per employer; a
  string list means any of these is right, `[]` means none exists and
  "unknown" is right. The posting text is read from the database by
  `vacancy_id`; the hh facts are snapshotted so the set does not drift.

  ```toml
  [[case]]
  vacancy_id = 10955
  note = "hh links the careers site"
  poster = { name = "ЭР-Телеком Холдинг", site_url = "http://rabotavdomru.ru", about = "Пермь; Телекоммуникации" }
  expect = { employer = ["ЭР-Телеком"], homepage = ["ertelecom.ru"], careers_page = ["rabotavdomru.ru"], is_agency = false, client = [] }
  ```

### Failure and cost

~1.5k tokens in, ~150 out at $0.10 / $0.40 per million → ~$0.0002 per
uncached call (an estimate; the eval measures it). An eval run of 23 cases is
~$0.005 per model. A hang ends at 30 s, retries once, ≤70 s, recorded as an
error row with what was spent. Invalid JSON or a refusal: paid, no identity
row, the raw response in the trace, and the next click pays again (the same
known gap as Jev's, `docs/architecture.md` §3.3). An all-null answer is not an
error. A hallucinated homepage gets `homepage_ok = false` and no link.

### Tests (offline)

The ledger: the migration fills `kind`; spend sums `match` only; `trace`
output. The call, on a mocked OpenRouter (`httpx.MockTransport` under the
OpenAI SDK): a good answer parsed and both rows written; invalid JSON and a
timeout write an error row with request and response; a cache hit makes no
HTTP call; the fingerprint changes with each input and the model; `reachable`
refuses private addresses and `javascript:`. The eval: `score` and the
normalisers on hand-built cases; `load_cases` rejects a case missing a field.
The paid eval run is a CLI command, never pytest. API tests with
`research.ask_employer` stubbed; `npm run typecheck`.

### Build steps

0. **Clear the bench** (done 2026-10-12): the sort work committed on its own;
   the LangChain attempt removed and its migration rolled back; the agents
   and `CLAUDE.md` committed.
1. **The ledger**: `kind`, `request`, `response`, the migration,
   `repository.record_call`, `jobmatch trace`; `docs/architecture.md` §3.3.
2. **The call** (built 2026-10-12): the research module, `employer_identities`, the
   pipeline and repository functions, `jobmatch identify --vacancy N`.
3. **The eval**: evals.py, `jobmatch eval employers`, the labels pre-filled
   for the user to correct; then gemini-2.5-flash-lite against
   mistral-small-2603, and `[models] employer` set from the scores.
4. **The card** (built 2026-10-13): the route, the schema, the UI. Built
   before the eval, by the user's choice, to see the answer on screen first;
   the eval (step 3) is still to come.

### Decisions

- LangChain and `ddgs`: dropped for the plain OpenAI SDK and, in step 2, a
  search inside the model call (`docs/decisions.md`, "People search").
- "Matcher protocol / pluggable LLM providers" stays refused: `research/` is
  plain modules, not an interface.
- "Per-vacancy files" holds: the eval file stores labels and short employer
  facts, not posting text.
- "`llm_calls`: nothing reads it to decide" holds: the cache is
  `employer_identities`; the ledger is for reading.

---

## 3. Step 2: recruiters on LinkedIn (approved 2026-10-13)

Designed by the designer agent, structure reviewed by the architect agent,
approved by the user.

### What the user sees

Redrawn 2026-10-15 with the vacancy page (one quiet column, no cards). Two
sections, Company and Recruiters (`frontend/src/components/CompanyPanel.vue`,
and `RecruitersBlock.vue` under it); each has its label on the left and its
actions on the right, the free one first.

**Company.**

1. Not looked up: the label and a quiet **Look up** (free, ~2 s, the hh
   lookup of §1). Once looked up, the same action reads **Refresh**.
2. Looked up: the links (hh's website host ↗ when hh has one, "hh profile ↗",
   and "LinkedIn ↗", LinkedIn's company search for the website's host in
   quotes, or the name without one), then a muted line: category ·
   industries · city. hh's category already says organization or agency,
   for free: `Organization` or `Recruitment agency` on a page read in
   English, `Организация` or `Кадровое агентство` on a row looked up before
   that, shown in hh's English words either way.
3. The identity call (§2, ~$0.0002, cached by its inputs) is offered only to
   fill a gap hh leaves: **Find website** when hh lists no site, **Find
   client** for an agency, **Identify** for both. Nothing is offered when hh
   already says it all. Its answer replaces the button: a homepage ↗ (when hh
   had none) and a careers page ↗, each only if it answered a GET and marked
   with a sparkles icon as the model's; for an agency (hh's category or the
   model's answer), "Recruitment agency · hiring for <client> · <city>" or
   "· client not named"; the real name, when it is not the posting's; the
   model's notes as a tooltip on each of those. An answer that adds none of
   them reads "Nothing found beyond hh". A failure reads "Couldn't check
   (<error>)" and the button stays.

**Recruiters**, once the company is looked up:

1. **Find recruiters** (~$0.01, 3–5 s measured): one AI web search for
   recruiters, HR and talent acquisition at the company. Once a search is
   shown, **Search again** always runs and pays for a new one (user,
   2026-10-14).
2. Each person, one row: the name, linking to the LinkedIn profile ↗ (its
   tooltip is the cited page's title and the profile slug, the check that
   the name matches the page), the role, and the written mark.
3. The written mark: "Mark as written", or "✓ Written <date>", which a click
   undoes. It is stored per profile in `recruiter_contacts`, so it outlives
   every search (`docs/architecture.md` §3.8).
4. Under the list, "Searched <date>"; nobody confirmed reads "No profiles
   found · searched <date>"; a failure, "Search failed (<error>)", above a
   stored list.

### Where the code lives

- `jobmatch/research/recruiters.py`: the question (`Target` in, `Recruiters`
  out, `RECRUITERS_SYSTEM`) and `keep_cited`, which keeps only the people
  whose profile the search cited.
- `jobmatch/company_research.py`: `target_of`, `find_recruiters` (the
  freshness rule, `RECRUITERS_FRESH`) and `stored_recruiters`, for the card.
- `[models] recruiters` in `config.toml`.
- `jobmatch/api/routers/company.py`: the route and its status codes.

### Data

**`recruiter_searches`**, per employer (the request holds no posting text):
`id`, `employer_id` (FK), `inputs_fingerprint` (not unique: the same request
is asked again once its answer is stale), `answer` (JSONB), `llm_call_id`
(FK), `created_at`; indexed on `(employer_id, created_at)`. Plain inserts; the
row shown is the newest for the employer with the request's fingerprint.

The user's decision, 2026-10-14: never pay twice for the same request, even
across postings of one company. A first build reused only the employer's
newest row, so a posting with an identity and one without (two requests)
paid in turn on every switch; the cache now looks the request up by its
fingerprint within the employer.

### Query by website, and refresh (the user's decisions, 2026-10-14)

- **The website identifies the company, not the name.** The prompt reads
  "Recruiters, HR or talent acquisition at the company whose website is
  ertelecom.ru (Пермь) — LinkedIn profiles"; the name is not sent when there
  is a website, and is used only when there is none. A name is shared by
  namesakes and spelt in two alphabets; a host is neither. `Target` keeps the
  name for the log. This changed every recruiter fingerprint: the two rows
  stored before it are not reused.
- **Which website.** Without an identity whose homepage answered
  (`homepage_ok`), the website is hh's link, often a careers or landing host
  (rabotavdomru.ru for ЭР-Телеком, l.tbank.ru for Т-Банк); an identity with
  a homepage that answered is what replaces it (`company_research.target_of`).
- **Chosen knowing the cost.** The user chose website-only knowing the
  search text then holds no name. Unmeasured: the first refreshes will show
  whether LinkedIn profiles still come back from a host-only search.
- **Refresh.** `jobmatch recruiters --vacancy N --refresh` and `?refresh=true`
  on the route skip the 30-day reuse and always pay, for a search the user
  doubts; a plain click or command still reuses a fresh row.

### Measured on the first clicks (2026-10-14)

Two searches (`jobmatch trace --kind recruiters`, calls 19578–9): strict
`json_schema` works with `:online`, citations arrive in `annotations` (5 per
search), and a search costs $0.007–0.008 in 3–5 s, not the ~$0.02 estimated.
УайтСнейк: 5 people kept, 0 dropped. ОнТаргет ЛАБС: 5 sources, nobody.
No committed eval file: it would hold recruiters' names.

### Build steps

1. **Make room** (built 2026-10-13, no behaviour change): the identify module
   becomes the research module; one call sends, validates and records any
   question (now `openrouter.ask`), and `ask_employer` is a thin wrapper over it.
2. **The question and the citation check** (built 2026-10-13, nothing calls
   it yet): the recruiters question (now `jobmatch/research/recruiters.py`) and its checks,
   with offline tests.
3. **The search, kept** (built 2026-10-13): the table, the `company_research` function,
   the `recruiters` model in config, and `jobmatch recruiters --vacancy N` for the
   first real search from the terminal.
4. **The block** (built 2026-10-14): the route, `CompanyRecruiters` on `GET`
   and `POST /company`, and `RecruitersBlock.vue` on the Company card.
