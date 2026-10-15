"""Who recruits for a company: a web search for LinkedIn profiles, keeping only the cited ones."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from ..llm import openrouter


@dataclass(frozen=True, slots=True)
class Target:
    """The company to search for; the name is kept for the log and UI even when the host is searched."""

    name: str | None
    homepage: str | None
    city: str | None

    @property
    def searchable(self) -> bool:
        """A website or a name to search for."""
        return bool(_host(self.homepage) or self.name)


# Prompt text: the docstrings and Field descriptions are sent, so editing one re-pays every search.
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
    """Query-like, naming the host over the name, which namesakes share (the user, 2026-10-14)."""
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
    """Exactly as sent; the web search is the model id's `:online` suffix, not a field."""
    return openrouter.request(RECRUITERS, target, model)


async def ask_recruiters(
    sent: dict[str, Any],           # `recruiters_request(target, model)`
    *,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,   # tests pass a mocked one
) -> tuple[Confirmed, int]:
    """The cited people and the `llm_calls` id; a failure is recorded, then raised."""
    answer, body, call_id = await openrouter.ask(
        RECRUITERS, sent, vacancy_id=vacancy_id, http_client=http_client
    )
    return keep_cited(answer, body), call_id


# --- keeping only the cited ---

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
    """A model invents plausible profile URLs; a citation proves the page exists, not the name on it."""
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
            linkedin_url=_profile_url(slug),
            evidence=person.evidence,
            source_title=citations[slug],
        )
    return Confirmed(
        people=tuple(kept.values()),
        dropped=dropped,
        sources=len(_url_citations(body)),
    )


def cited(body: dict[str, Any]) -> dict[str, str | None]:
    """slug -> title, from `annotations` only: a URL in the content is a claim, not a citation."""
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
    """`https://ru.linkedin.com/in/Ivan-Petrov/` -> `ivan-petrov`, unquoted so Cyrillic compares equal."""
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
    # What percent-encoding can smuggle in.
    if not slug or "/" in slug or any(c.isspace() for c in slug) or set(slug) == {"."}:
        return None
    return slug


def linkedin_profile_url(url: str) -> str | None:
    """The one form a profile is stored and marked under, as the model's URLs vary."""
    slug = linkedin_slug(url)
    return None if slug is None else _profile_url(slug)


def _profile_url(slug: str) -> str:
    """Quoted, so a slug's own text never reaches an href unescaped."""
    return f"https://www.linkedin.com/in/{quote(slug, safe='')}"
