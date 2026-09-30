"""The source seam.

A source owns everything about its site — URLs, headers, parsing, id
extraction, normalisation. Outside this package only ``Listing`` and
``VacancyData`` are known, so adding a site is a new module plus one entry in
``SOURCES``.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Crawl:
    """How hard a source may walk a paginated listing.

    Not search parameters — those are opaque and go to the site verbatim.
    These are the two knobs that stop a crawl being rude or endless, and every
    paginated source needs both.
    """

    max_pages: int = 5
    delay: float = 2.5


@dataclass(frozen=True, slots=True)
class Rate:
    """How fast a source's *detail pages* may be fetched.

    Per source, not global: throttling tolerance belongs to the site. The rate
    is ``workers / delay`` a second -- each worker sleeps ``delay`` after its own
    page, so raising workers alone raises the rate.
    """

    workers: int = 1
    delay: float = 1.0


class VacancyGone(Exception):
    """A positive signal that the vacancy is no longer listed (404, archived).

    Distinct from any other fetch failure: this one is the employer's doing and
    is never worth retrying, so the pipeline can act on it without knowing
    anything about HTTP.
    """


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
    raw: dict[str, Any] = field(default_factory=dict)  # -> vacancies.raw


@dataclass(frozen=True, slots=True)
class Source:
    """A site: a search feed, a page parser, and the limits it imposes.

    ``crawl`` and ``rate`` are data, not behaviour: what this site tolerates,
    measured against it, so no config has to restate them.
    """

    name: str
    hosts: tuple[str, ...]                 # which URLs this source owns
    discover: Callable[..., Iterable[Listing]]
    fetch: Callable[[str], VacancyData]    # a URL, not a Listing: `fetch <url>` has no feed
    crawl: Crawl = Crawl()                 # how far its listing may be walked
    rate: Rate = Rate()                    # how fast its pages may be fetched


from . import hh  # noqa: E402  (circular-free: hh imports only the dataclasses)

SOURCES: dict[str, Source] = {s.name: s for s in (hh.SOURCE,)}


def source_for_url(url: str) -> Source:
    """Pick the source that owns ``url``, by host."""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    for source in SOURCES.values():
        if host in source.hosts:
            return source
    raise ValueError(f"No source handles {url!r}")
