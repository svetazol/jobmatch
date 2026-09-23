"""The source seam.

A source owns everything about its site — URLs, headers, parsing, id
extraction, normalisation. Outside this package only ``VacancyData`` is known
(``Listing`` joins it with discovery), so adding a site is a new module plus
one entry in ``SOURCES``.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


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
    skills: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # -> vacancies.raw


@dataclass(frozen=True, slots=True)
class Source:
    """A site. ``discover`` joins it when the pipeline needs many vacancies."""

    name: str
    hosts: tuple[str, ...]
    fetch: Callable[[str], VacancyData]


from . import hh  # noqa: E402  (circular-free: hh imports only the dataclasses)

SOURCES: dict[str, Source] = {s.name: s for s in (hh.SOURCE,)}


def source_for_url(url: str) -> Source:
    """Pick the source that owns ``url``, by host."""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    for source in SOURCES.values():
        if host in source.hosts:
            return source
    raise ValueError(f"No source handles {url!r}")
