"""Questions to a model, through OpenRouter, about a company; the only module
that knows the OpenAI SDK.

As `matching/jev.py` is the only one that knows TypeSafe's. Each question is
one structured-output request: a pydantic model is the one shape of its
answer -- its JSON schema is sent as the response format, it validates what
comes back. Today there are two questions. Who is behind a posting: the poster's hh facts and the posting
go in, an `Identity` comes out -- the employer, its homepage and careers page,
and whether the poster is a recruiting agency. And who recruits for it: a web
search for their LinkedIn profiles, of which only those the search itself
cited are kept (`keep_cited`). An `Identity`'s `model_dump()` is stored as
is; a `Recruiters` answer is stored only as the `Confirmed` that `keep_cited`
leaves.

Every call goes through `_call`, which writes it to `llm_calls` with the
request and the body as received, whether it succeeded or not. The caches are
the pipeline's (`pipeline.identify_employer`); this module always pays.

Design and reasoning: `docs/company-research.md` §2 (and §3 for the recruiters
search).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import llm
# The hashing rule is llm's; callers still say `research.fingerprint`.
from .llm import fingerprint

BASE_URL = "https://openrouter.ai/api/v1"

MODEL_DEADLINE = 30     # one model request, whole; see _Deadline
MODEL_RETRIES = 1       # so a hang costs at most two deadlines

REACH_DEADLINE = 10     # one reachability check, whole, redirects included
MAX_REDIRECTS = 3       # followed by hand, so each hop's address is checked
# A browser's, because some homepages answer a library's User-Agent with 403
# and would read as "did not answer".
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
}

# OpenRouter's own request fields, part of every request: the call's cost in
# `usage`, and only providers that honour every parameter -- without that, one
# that ignores `response_format` may answer in free text.
OPENROUTER = {"usage": {"include": True}, "provider": {"require_parameters": True}}


# --- who is behind a posting ----------------------------------------------------

@dataclass(frozen=True, slots=True)
class Poster:
    """What is known about whoever posted the vacancy, before asking."""

    name: str | None        # as the hh employer page gives it
    site_url: str | None    # the link that page publishes; often a careers site
    about: str | None       # hh's city and industries: tells namesakes apart
    posting: str            # the vacancy as Jev reads it (`matching.job_text`)


# The docstring and every Field description are sent: pydantic puts them in the
# schema, so they are written for the model, and editing one changes every
# employer fingerprint (one cache miss per stored identity). Notes for people
# go here instead:
# every field is required and nothing extra is allowed, which OpenAI-style
# `strict` structured output demands; pydantic already writes `X | None` as
# `anyOf` with null and `extra="forbid"` as `additionalProperties: false`, so
# the generated schema is sent unchanged (`tests/test_research.py` holds it to
# that).
class Identity(BaseModel):
    """Who is behind the posting. Null means unknown, never "there is none"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    employer: str | None = Field(
        description="The organisation that posted the vacancy, by its common name; "
        "for a division, the parent company"
    )
    homepage: str | None = Field(description="That organisation's main website, a full URL")
    careers_page: str | None = Field(description="Its careers or jobs site, a full URL")
    is_agency: bool | None = Field(
        description="True if the poster is a recruiting agency hiring for a client"
    )
    client: str | None = Field(
        description="An agency's client, only if the posting names it"
    )
    notes: str = Field(description="One or two sentences: how you decided, what is unsure")

    @field_validator("homepage", "careers_page")
    @classmethod
    def _with_scheme(cls, url: str | None) -> str | None:
        """`ertelecom.ru` -> `https://ertelecom.ru`, so the stored answer, the
        reachability check and the UI's link are one URL. A model asked for a
        full URL still answers with a bare domain at times; the raw answer stays
        in `llm_calls.response`. Anything that is not then http(s) -- a
        `javascript://` the UI would run on click -- is stored as unknown, as
        hh's `_web_url` does for its link. The schema is unchanged by this."""
        if url and "://" not in url:
            url = f"https://{url}"
        if url and urlsplit(url).scheme in ("http", "https"):
            return url
        return None


EMPLOYER_SYSTEM = """\
You identify the organisation behind a job posting from hh.ru. Use only the
facts given and what you already know about real companies. For anything you
do not know, answer null: a null is useful, a guess is not.

- The link from the hh.ru page is whatever the poster chose to publish. It is
  often a careers or HR site (https://yandex.ru/jobs, https://rabota.vtb.ru)
  rather than the homepage; put each where it belongs, as a full URL.
- A posting from a division ("Банк ВТБ (ПАО), Технологический блок") is the
  parent company's ("ВТБ").
- A recruiting agency says so: "наш клиент", "для нашего клиента", "кадровое
  агентство". Then the employer is the agency, and the client is named only if
  the posting names it. Never infer a client from a description such as
  "крупный банк" or "международная IT-компания"."""


def employer_prompt(poster: Poster) -> str:
    return "\n".join([
        f"Name on hh.ru: {poster.name or 'unknown'}",
        f"Link on its hh.ru page: {poster.site_url or 'none'}",
        f"About, from hh.ru: {poster.about or 'unknown'}",
        "",
        "The posting:",
        "",
        poster.posting,
    ])


def employer_request(poster: Poster, model: str) -> dict[str, Any]:
    """The whole request, exactly as sent: stored as `llm_calls.request`, and
    what `fingerprint` hashes."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": EMPLOYER_SYSTEM},
            {"role": "user", "content": employer_prompt(poster)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "employer_identity", "strict": True,
                "schema": Identity.model_json_schema(),
            },
        },
        **OPENROUTER,
    }


async def ask_employer(
    poster: Poster,
    model: str,
    *,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,   # tests pass a mocked one
) -> tuple[Identity, int]:
    """Who is behind a posting: the answer and its `llm_calls` id. On any
    failure the call is recorded, then the error raised."""
    identity, _, call_id = await _call(
        employer_request(poster, model), Identity,
        kind="employer", vacancy_id=vacancy_id, http_client=http_client,
    )
    return identity, call_id



# --- who recruits for it ---------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Target:
    """The company to search for: its homepage, its name, and its city.

    The prompt names it by the homepage's host where there is one, by the name
    only where there is not (`recruiters_prompt`); the name is kept either way
    for the log and the UI.
    """

    name: str | None
    homepage: str | None
    city: str | None

    @property
    def searchable(self) -> bool:
        """Whether there is a website or a name to search for."""
        return bool(_host(self.homepage) or self.name)


# As with `Identity`, the docstrings and descriptions are prompt text, covered
# by the fingerprint (one cache miss per stored search on any edit). Notes for
# people: `linkedin_url` and `evidence` are what `keep_cited` checks against
# the search's citations; nothing the model writes here is trusted unchecked.
class Person(BaseModel):
    """Someone who works at the company now, in recruiting, HR or talent acquisition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Their name, as the search result gives it")
    role: str | None = Field(description="Their job title, as the search result gives it")
    linkedin_url: str = Field(
        description="Their LinkedIn profile (linkedin.com/in/...), "
        "copied exactly from a search result link"
    )
    evidence: str = Field(
        description="The search result text that shows their name, role and company"
    )


class Recruiters(BaseModel):
    """The people found. An empty list is a good answer; a guess is not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    people: list[Person] = Field(description="Each person the search results show")
    notes: str = Field(description="One or two sentences: what you searched, what is unsure")


RECRUITERS_SYSTEM = """\
You find the people who recruit for a company: recruiters, HR and talent
acquisition who work there now. Search the web for their LinkedIn profiles.

- Use only what the web search returned. Every profile must be a
  linkedin.com/in/... page that a search result links; never write a URL from
  memory or build one from a name.
- `evidence` is the search result text that shows the person's name, role and
  company.
- The company is given by its website where it has one. Keep only people
  the search results tie to the company behind that site; a company with a
  similar name is not it: tell them apart by the website and the city.
- Someone who left the company, or who recruits for another one, does not
  belong in the list.
- If the search shows no one, answer with an empty list: an empty list is
  useful, a guess is not."""


def recruiters_prompt(target: Target) -> str:
    """Short and query-like: the web search is likely built from this text.

    The company is named by its website's bare host, as a person would type
    it, and by its name only when it has no website: a name is shared by
    namesakes and spelt in two alphabets, a host is neither (the user,
    2026-10-14). Only for a `searchable` target.
    """
    host = _host(target.homepage)
    company = f"the company whose website is {host}" if host else target.name
    if target.city:
        company = f"{company} ({target.city})"
    return f"Recruiters, HR or talent acquisition at {company} — LinkedIn profiles"


def _host(url: str | None) -> str | None:
    """`https://www.ertelecom.ru/about` -> `ertelecom.ru`."""
    try:
        host = urlsplit(url).hostname if url else None
    except ValueError:
        return None
    return host.removeprefix("www.") if host else None


def recruiters_request(target: Target, model: str) -> dict[str, Any]:
    """The whole request, exactly as sent, as `employer_request`. The web
    search is the model id's `:online` suffix, not a field here."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": RECRUITERS_SYSTEM},
            {"role": "user", "content": recruiters_prompt(target)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "recruiters", "strict": True,
                "schema": Recruiters.model_json_schema(),
            },
        },
        **OPENROUTER,
    }


async def ask_recruiters(
    target: Target,
    model: str,
    *,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,   # tests pass a mocked one
) -> tuple[Confirmed, int]:
    """Who recruits for the company: the people the search's own citations
    confirm, and the call's `llm_calls` id. On any failure the call is
    recorded, then the error raised."""
    answer, body, call_id = await _call(
        recruiters_request(target, model), Recruiters,
        kind="recruiters", vacancy_id=vacancy_id, http_client=http_client,
    )
    return keep_cited(answer, body), call_id

# --- checks on an answer ----------------------------------------------------------

async def reachable(url: str, *, http_client: httpx2.AsyncClient | None = None) -> bool:
    """Does `url` answer below 400, within `REACH_DEADLINE`, in at most
    `MAX_REDIRECTS` hops, each of them public? Never raises.

    A hallucinated homepage usually does not resolve; this is what keeps it
    from being shown as a link. It does not check that the page is about the
    employer: a Cyrillic name against a Latin site would fail real homepages.
    """
    try:
        async with asyncio.timeout(REACH_DEADLINE):
            async with http_client or httpx2.AsyncClient(
                headers=HEADERS, follow_redirects=False
            ) as client:
                return await _answers(client, url)
    except (TimeoutError, httpx2.HTTPError, ValueError):   # ValueError: a malformed URL
        return False


async def _answers(client: httpx2.AsyncClient, url: str) -> bool:
    for _ in range(MAX_REDIRECTS + 1):
        if not await _public(url):
            return False
        async with client.stream("GET", url) as resp:   # the body is never read
            if resp.is_redirect and (location := resp.headers.get("location")):
                url = urljoin(url, location)
                continue
            return resp.status_code < 400
    return False                                        # too many redirects


async def _public(url: str) -> bool:
    """http(s), and every address the host resolves to is public.

    The URL comes from a model's answer, and this machine answers on localhost
    with CV-derived data (the API on :8000). The check resolves the name
    itself, so a public hostname pointing at 127.0.0.1 is refused too. (The
    HTTP client resolves again on connect; a rebinding DNS server could slip
    between the two, which is beyond what a single-user tool needs to stop.)
    """
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)



@dataclass(frozen=True, slots=True)
class ConfirmedPerson:
    """A `Person` whose profile the search cited; what is stored and shown."""

    name: str
    role: str | None
    linkedin_url: str           # canonical: https://www.linkedin.com/in/<slug>
    evidence: str               # the model's quote of the result
    source_title: str | None    # the cited page's title, as the search gave it


@dataclass(frozen=True, slots=True)
class Confirmed:
    """What survived `keep_cited`, with counts the card shows."""

    people: tuple[ConfirmedPerson, ...]
    dropped: int                # people the model named whose profile was not cited
    sources: int                # url_citation annotations: how much the search found


def keep_cited(answer: Recruiters, body: dict[str, Any]) -> Confirmed:
    """Only the people whose profile the web search itself returned. Never raises.

    A model asked for LinkedIn URLs writes plausible ones from a name; a
    citation is OpenRouter's record of a page the search really returned, so
    it is the one check on a profile that costs nothing. Each person is kept
    once, by slug; a repeat is not counted as dropped.

    The trust boundary: a citation proves the profile exists and the search
    returned it, not that the model's name and role belong to it. The
    `source_title` is the search's own text for that page, which is why the
    card shows it beside the name.
    """
    citations = cited(body)
    kept: dict[str, ConfirmedPerson] = {}
    dropped = 0
    for person in answer.people:
        slug = linkedin_slug(person.linkedin_url)
        if slug is None or slug not in citations:
            dropped += 1
            continue
        if slug in kept:
            continue
        kept[slug] = ConfirmedPerson(
            name=person.name,
            role=person.role,
            linkedin_url=f"https://www.linkedin.com/in/{quote(slug, safe='')}",
            evidence=person.evidence,
            source_title=citations[slug],
        )
    return Confirmed(
        people=tuple(kept.values()),
        dropped=dropped,
        sources=len(_url_citations(body)),
    )


def cited(body: dict[str, Any]) -> dict[str, str | None]:
    """LinkedIn profile slug -> the citation's title, for every profile the
    search cited. Reads only the answer's `annotations`: a URL the model
    wrote in its content is a claim, not a citation."""
    found: dict[str, str | None] = {}
    for citation in _url_citations(body):
        url, title = citation.get("url"), citation.get("title")
        slug = linkedin_slug(url) if isinstance(url, str) else None
        if slug is not None and found.get(slug) is None:
            found[slug] = title if isinstance(title, str) else None
    return found


def _url_citations(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Each `url_citation` object in the first choice; any other shape is none."""
    try:
        annotations = body["choices"][0]["message"].get("annotations") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return []
    if not isinstance(annotations, list):
        return []
    return [
        a["url_citation"] for a in annotations
        if isinstance(a, dict) and a.get("type") == "url_citation"
        and isinstance(a.get("url_citation"), dict)
    ]


def linkedin_slug(url: str) -> str | None:
    """`https://ru.linkedin.com/in/Ivan-Petrov/?trk=x` -> `ivan-petrov`; None for
    anything but a personal profile on LinkedIn's own host.

    The slug is unquoted and lowercased, so a model's Cyrillic and a
    citation's percent-encoding of it compare equal. The host must be
    linkedin.com or a subdomain of it: `linkedin.com.evil.io` is not. A slug
    with a slash or a blank in it, or only dots, is none.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:                                   # e.g. a malformed IPv6 host
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    path = [segment for segment in parts.path.split("/") if segment]
    if len(path) < 2 or path[0] != "in":
        return None
    slug = unquote(path[1]).lower()
    # what a percent-encoding can smuggle in: a path of its own, or a blank
    if not slug or "/" in slug or any(c.isspace() for c in slug) or set(slug) == {"."}:
        return None
    return slug

# --- the call ------------------------------------------------------------------

async def _call[T: BaseModel](
    request: dict[str, Any],
    answer: type[T],
    *,
    kind: str,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,
) -> tuple[T, dict[str, Any], int]:
    """Send once, validate into `answer`, record the call; the answer, the body as received, and its row id.

    `create`, not the SDK's `parse`: a body that fails validation was still paid for, so it is kept for the trace.
    """
    key = llm.api_key("before asking a model about a company")
    async with llm.recorded(
        kind=kind, vacancy_id=vacancy_id, fingerprint=fingerprint(request),
        model=request["model"], request=request,
    ) as call:
        body = await _send(request, key, http_client)
        call.response, call.usage = body, _usage(body)
        parsed = answer.model_validate_json(_content(body))
    return parsed, body, call.id


async def _send(
    request: dict[str, Any], key: str, http_client: httpx2.AsyncClient | None
) -> dict[str, Any]:
    """One chat completion; the response body as received.

    Everything but `model` and `messages` goes as the SDK's `extra_body`, so
    the request is sent exactly as stored, whatever fields it holds; the SDK
    adds none. `test_the_request_asks_for_strict_json_from_providers_that_
    honour_it` compares the stored request with the body OpenRouter received.
    """
    model, messages = request["model"], request["messages"]
    rest = {k: v for k, v in request.items() if k not in ("model", "messages")}
    async with AsyncOpenAI(
        api_key=key,
        base_url=BASE_URL,
        max_retries=MODEL_RETRIES,
        http_client=http_client or _Deadline(),
    ) as client:
        raw = await client.chat.completions.with_raw_response.create(
            model=model, messages=messages, extra_body=rest,
        )
    return raw.http_response.json()


def _content(body: dict[str, Any]) -> str:
    """The answer's text. A refusal or an empty choice has none, and is an
    error like invalid JSON: paid for, nothing to store."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not content:
        raise ValueError("the response holds no answer; see its body in the trace")
    return content


def _usage(body: dict[str, Any]) -> llm.Usage:
    """OpenRouter's usage, from the body: the cost is its own field, not the SDK's."""
    usage = body.get("usage") or {}
    cost = usage.get("cost")
    return llm.Usage(
        model=body.get("model"),
        provider=body.get("provider"),
        response_id=body.get("id"),
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cost_usd=None if cost is None else Decimal(str(cost)),
    )


class _Deadline(httpx2.AsyncClient):
    """The OpenAI SDK's client, with a deadline on each whole request.

    Measured 2026-10-07: while the upstream model works, OpenRouter sends
    eleven bytes of whitespace every 3 s, so a per-read timeout never fires and
    a stuck call waits indefinitely. Past the deadline this raises the client's
    own `ReadTimeout`, which the SDK retries (`max_retries`) like any other.
    """

    async def send(self, request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
        try:
            async with asyncio.timeout(MODEL_DEADLINE):
                return await super().send(request, **kwargs)
        except TimeoutError:
            raise httpx2.ReadTimeout(
                f"no answer within {MODEL_DEADLINE}s", request=request
            ) from None
