"""The source seam: a site is one module plus an entry in ``SOURCES``; only these dataclasses leave it."""
from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Crawl:
    """The two knobs that stop a paginated crawl being rude or endless."""

    max_pages: int = 5
    delay: float = 2.5


@dataclass(frozen=True, slots=True)
class Rate:
    """Detail pages per second is ``workers / delay``, per process: two processes double it."""

    workers: int = 1
    delay: float = 1.0


class VacancyGone(Exception):
    """A positive 404/archived signal, never worth retrying, so the pipeline needs no HTTP."""


class EmployerGone(Exception):
    """The site has no page for this employer id (404)."""


@dataclass(frozen=True, slots=True)
class Listing:
    """What a search feed knows about a vacancy: enough to fetch it later."""

    external_id: str
    url: str
    title: str | None = None
    published_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class VacancyData:
    """A parsed vacancy page, already normalised by the source."""

    external_id: str
    url: str
    title: str
    description: str  # plain text, de-HTML'd by the source
    company: str | None = None
    salary: str | None = None
    experience: str | None = None
    published_at: dt.datetime | None = None
    country: str | None = None
    skills: list[str] = field(default_factory=list)
    work_formats: list[str] = field(default_factory=list)   # a set: onsite/remote/hybrid
    # None when the posting hides it.
    employer_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # -> vacancies.raw


@dataclass(frozen=True, slots=True)
class EmployerData:
    """A parsed employer page, fetched only when someone asks about the company."""

    external_id: str
    url: str
    name: str | None = None
    site_url: str | None = None    # what the employer itself links to; can be a careers page
    raw: dict[str, Any] = field(default_factory=dict)  # -> employers.raw


@dataclass(frozen=True, slots=True)
class Source:
    """A site; ``crawl`` and ``rate`` are measured against it, so no config restates them."""

    name: str
    hosts: tuple[str, ...]                 # which URLs this source owns
    discover: Callable[..., AsyncIterator[Listing]]
    fetch: Callable[[str], Awaitable[VacancyData]]  # a URL, not a Listing: `fetch <url>` has no feed
    crawl: Crawl = Crawl()
    rate: Rate = Rate()
    # None for a site without employer pages.
    fetch_employer: Callable[[str], Awaitable[EmployerData]] | None = None


from . import hh  # noqa: E402  (circular-free: hh imports only the dataclasses)

SOURCES: dict[str, Source] = {s.name: s for s in (hh.SOURCE,)}


def source_for_url(url: str) -> Source:
    """Pick the source that owns ``url``, by host."""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    for source in SOURCES.values():
        if host in source.hosts:
            return source
    raise ValueError(f"No source handles {url!r}")
