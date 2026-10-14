"""Who recruits for a company: a web search for LinkedIn profiles, of which only the cited ones are kept."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from ..llm import openrouter


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


RECRUITERS = openrouter.Question(
    kind="recruiters", schema_name="recruiters",
    system=RECRUITERS_SYSTEM, prompt=recruiters_prompt, answer=Recruiters,
)


def recruiters_request(target: Target, model: str) -> dict[str, Any]:
    """The whole request, exactly as sent; the web search is the model id's `:online` suffix, not a field."""
    return openrouter.request(RECRUITERS, target, model)


async def ask_recruiters(
    sent: dict[str, Any],           # `recruiters_request(target, model)`
    *,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,   # tests pass a mocked one
) -> tuple[Confirmed, int]:
    """The people the search's own citations confirm, and the call's `llm_calls` id; a failure is recorded, then raised."""
    answer, body, call_id = await openrouter.ask(
        RECRUITERS, sent, vacancy_id=vacancy_id, http_client=http_client
    )
    return keep_cited(answer, body), call_id


# --- keeping only the cited --------------------------------------------------------

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
