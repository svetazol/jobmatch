"""hh.ru — everything hh-shaped lives in this module.

Parsing is the prototype's ``data-qa`` approach with a JSON-LD fallback for
branded layouts. The JSON-LD ``JobPosting`` block is present alongside the
normal layout too, so it is always read: it carries ``datePosted`` and is kept
whole in ``VacancyData.raw``.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from . import Listing, Source, VacancyData, VacancyGone

NAME = "hh.ru"
# The feed returns the latest 20 items and does not paginate: page, per_page
# and items_on_page are all ignored. The corpus grows by running repeatedly,
# which the (source, external_id) upsert makes free.
RSS_URL = "https://hh.ru/search/vacancy/rss"
# One HeadHunter, several domains: headhunter.ge (Georgia) serves the same
# engine, the same layout and the same vacancy ids as hh.ru, so a vacancy
# reached through either domain is one row. Which country's vacancies you get
# is the `area` search param, not the domain — the .ge feed without it returns
# Moscow. Only verified domains are listed; add others when one is needed.
HOSTS = ("hh.ru", "headhunter.ge")
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

_VACANCY_ID = re.compile(r"/vacancy/(\d+)")
# The site prints work formats as one comma-and-"or" separated line, which can
# name up to three at once. The keys below are the site's own wording and must
# stay spelled as it spells them; an unrecognised phrase is kept verbatim
# rather than dropped, so a new one shows up in the data instead of vanishing.
_WORK_FORMAT_PREFIX = "Формат работы:"
_WORK_FORMAT_SPLIT = re.compile(r",\s*|\s+или\s+")
# The site names the country in its own language; the JSON-LD also carries an
# ISO 3166-1 alpha-2 code, which is what this maps to an English name. These
# are hh's nine top-level areas. A country outside them — the "other regions"
# area can return any — is stored as its bare ISO code, which is visible and
# obviously unmapped rather than silently wrong.
COUNTRY_NAMES = {
    "RU": "Russia",
    "UA": "Ukraine",
    "KZ": "Kazakhstan",
    "AZ": "Azerbaijan",
    "BY": "Belarus",
    "GE": "Georgia",
    "KG": "Kyrgyzstan",
    "UZ": "Uzbekistan",
}

WORK_FORMATS = {
    "на месте работодателя": "onsite",
    "удалённо": "remote",
    "удаленно": "remote",
    "гибрид": "hybrid",
    "разъездной": "field_work",
}
_SPACES = re.compile(r"[^\S\n]+")
_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")


class ParseError(RuntimeError):
    """The page loaded but doesn't look like a vacancy any more."""


def canonical_url(url: str) -> str:
    """Drop tracking params and the region subdomain's query noise."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))


def vacancy_id(url: str) -> str:
    match = _VACANCY_ID.search(url)
    if not match:
        raise ValueError(f"No hh.ru vacancy id in {url!r}")
    return match.group(1)


def _clean(value: str | None, *, inline: bool = False) -> str | None:
    """Collapse nbsp and runs of whitespace; scraped text is full of both.

    ``inline=True`` folds newlines too, for one-line fields like the salary
    (``'\u0434\u043e\\n5\\xa0500\\n$\\n\u0437\u0430\\xa0\u043c\u0435\u0441\u044f\u0446'``).
    """
    if value is None:
        return None
    text = value.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    if inline:
        text = _WHITESPACE.sub(" ", text)
    else:
        text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
        text = _BLANK_LINES.sub("\n\n", text)
    return text.strip() or None


def _text_of(soup: BeautifulSoup, qa: str) -> str | None:
    el = soup.find(attrs={"data-qa": qa})
    return el.get_text("\n", strip=True) if el else None


def _json_ld(soup: BeautifulSoup) -> dict | None:
    """hh.ru embeds a schema.org JobPosting block on every vacancy page."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def _work_formats(value: str | None) -> list[str]:
    value = _clean(value, inline=True)
    if not value:
        return []
    value = value.removeprefix(_WORK_FORMAT_PREFIX).strip()
    return [
        WORK_FORMATS.get(part.lower(), part.lower())
        for raw_part in _WORK_FORMAT_SPLIT.split(value)
        if (part := raw_part.strip())
    ]


def _country(ld: dict) -> str | None:
    """The country as an English name, from the posting's own ISO code.

    ``jobLocation.address.addressCountry`` is ISO alpha-2. It cannot be
    replaced by the searched area code: the "other regions" area is a catch-all
    that returns whichever country the job is actually in. Both the raw code
    and hh's own localised name stay in ``raw`` for anything that wants a flag
    or the original wording.
    """
    address = (ld.get("jobLocation") or {}).get("address") or {}
    code = _clean(address.get("addressCountry"), inline=True)
    if not code:
        return None
    return COUNTRY_NAMES.get(code.upper(), code.upper())


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def parse(html: str, url: str) -> VacancyData:
    """Pure parse, so tests can run off a saved page with no network."""
    soup = BeautifulSoup(html, "lxml")
    ld = _json_ld(soup) or {}

    title = _clean(_text_of(soup, "vacancy-title"), inline=True)
    company = _clean(_text_of(soup, "vacancy-company-name"), inline=True)
    description = _clean(_text_of(soup, "vacancy-description"))

    # Branded vacancies use a different layout; JSON-LD is the fallback.
    if not description and ld:
        title = title or _clean(ld.get("title"), inline=True)
        company = company or _clean(
            (ld.get("hiringOrganization") or {}).get("name"), inline=True
        )
        description = _clean(
            BeautifulSoup(ld.get("description") or "", "lxml").get_text("\n", strip=True)
        )

    if not description or not title:
        raise ParseError(f"No description found at {url}; the layout may have changed")

    return VacancyData(
        external_id=vacancy_id(url),
        url=url,
        title=title,
        description=description,
        company=company,
        salary=_clean(_text_of(soup, "vacancy-salary"), inline=True),
        experience=_clean(_text_of(soup, "vacancy-experience"), inline=True),
        published_at=_parse_date(ld.get("datePosted")),
        country=_country(ld),
        work_formats=_work_formats(_text_of(soup, "work-formats-text")),
        skills=[
            text
            for s in soup.find_all(attrs={"data-qa": re.compile(r"^skills-element")})
            if (text := _clean(s.get_text(strip=True), inline=True))
        ],
        raw={"json_ld": ld} if ld else {},
    )


def discover(params: Mapping[str, Any], delay: float = 1.0) -> Iterator[Listing]:
    """Whatever the feeds currently hold — 20 items each, no paging.

    A list of ``area`` values becomes one request *per area*, because the
    20-item cap is per request: nine areas in a single request would still
    return 20 items in total, not 180. How many requests a filter costs is
    hh's business, not the pipeline's — same seam as pagination.
    """
    params = dict(params)
    areas = params.pop("area", None)
    area_list = list(areas) if isinstance(areas, (list, tuple)) else [areas]
    for index, area in enumerate(area_list):
        if index:
            time.sleep(delay)
        query = params if area is None else {**params, "area": area}
        yield from _feed(query)


def _feed(params: Mapping[str, Any]) -> Iterator[Listing]:
    resp = requests.get(RSS_URL, params=dict(params), headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    for item in BeautifulSoup(resp.text, "xml").find_all("item"):
        if not (link := item.find("link")):
            continue
        url = canonical_url(link.get_text(strip=True))
        title = item.find("title")
        pub = item.find("pubDate")
        yield Listing(
            external_id=vacancy_id(url),
            url=url,
            title=_clean(title.get_text() if title else None, inline=True),
            # provisional: fetch replaces it with the JSON-LD datePosted
            published_at=_parse_date(pub.get_text(strip=True) if pub else None),
        )


def fetch(url: str) -> VacancyData:
    url = canonical_url(url)
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if resp.status_code in (404, 410):
        raise VacancyGone(f"{resp.status_code} for {url}")
    resp.raise_for_status()
    return parse(resp.text, url)


SOURCE = Source(name=NAME, hosts=HOSTS, discover=discover, fetch=fetch)
