"""hh.ru — everything hh-shaped lives in this module.

Parsing is the prototype's ``data-qa`` approach with a JSON-LD fallback for
branded layouts. The JSON-LD ``JobPosting`` block is present alongside the
normal layout too, so it is always read: it carries ``datePosted`` and is kept
whole in ``VacancyData.raw``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
from bs4 import BeautifulSoup

from .. import Crawl, Listing, Rate, Source, VacancyData, VacancyGone
from . import areas

log = logging.getLogger(__name__)

NAME = "hh.ru"

# What hh tolerates -- measured, not configured. Rate is workers/delay per
# second; 2026-09-24 at 6 workers hh answered 200 with a stripped page and 597
# vacancies came back empty. 3 at 1.0s held. workers=1 is sequential.
RATE = Rate(workers=3, delay=1.0)

# 2026-09-29: search pages 0-39 at 50 results, 404 from page 40 -- 2 000 per
# query, so 40 asks for all hh will give. Result pages are ~1.2MB, hence the
# larger delay than for vacancy pages.
CRAWL = Crawl(max_pages=40, delay=2.5)
RESULT_CEILING = 2000

# Coverage, not preference: under the cap, ordering decides *which* 2 000 you
# get, and newest-first is the only ordering a repeated crawl converges under.
# A configured `order_by` still wins.
ORDER_BY = "publication_time"
# The paginated HTML results, not the RSS feed: RSS serves only the newest 20
# per query, where the same filter has thousands here. 50 per page,
# `page=0,1,...`; `items_on_page` is ignored. (The *markup* shows only the first
# 20 -- see `_listings` -- but the embedded state carries all 50.)
SEARCH_URL = "https://hh.ru/search/vacancy"
# Present on any real results page, including the one past the last result.
# Its *absence* is how a throttled response gives itself away -- see _search_page.
SEARCH_HEADER = "vacancies-search-header"
SEARCH_ITEM = "serp-item__title"
SEARCH_ATTEMPTS = 3
FETCH_ATTEMPTS = 3       # a vacancy page re-asked past the throttling stub
THROTTLE_BACKOFF = 3.0   # seconds, multiplied by the attempt number
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


def _client() -> httpx2.AsyncClient:
    """One HTTP client per walk or per page; the only place they are made.

    ``follow_redirects`` keeps what ``requests`` did by default. A function
    rather than a module-level client, because a client belongs to the event
    loop it was opened on -- and so tests can hand back one on a
    ``MockTransport``.
    """
    return httpx2.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)


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


class Throttled(RuntimeError):
    """hh served its stripped page instead of content, repeatedly.

    Raised for both a search page and a vacancy page: same 200-with-no-content
    behaviour, same meaning — slow down.
    """


# the search path's older name, kept so nothing importing it breaks
SearchThrottled = Throttled


async def discover(
    params: Mapping[str, Any],
    crawl: Crawl | None = None,
    countries: Sequence[str] = (),
) -> AsyncIterator[Listing]:
    """Walk the search results, one query at a time.

    ``countries`` holds plain names like ``russia``, each expanded by
    ``areas.coverage`` into the queries it takes to get under ``RESULT_CEILING``.
    Without them, an ``area`` in ``params`` is walked directly, one query per
    entry -- the path the parser tests use. ``crawl`` defaults to ``CRAWL``.

    Yields each vacancy once: queries overlap when postings shift between
    requests, and counting one twice would misreport the corpus.
    """
    crawl = crawl or CRAWL
    base: dict[str, Any] = {"order_by": ORDER_BY, **dict(params)}
    if countries:
        queries = [query for country in countries for query in areas.coverage(country)]
    else:
        spec = base.pop("area", None)
        listed = spec if isinstance(spec, (list, tuple)) else [spec]
        queries = [{} if area is None else {"area": area} for area in listed]

    seen: set[str] = set()
    async with _client() as client:     # one connection pool for the whole sweep
        for query in queries:
            async for listing in _walk(client, {**base, **query}, crawl):
                if listing.external_id not in seen:
                    seen.add(listing.external_id)
                    yield listing


async def _walk(
    client: httpx2.AsyncClient, params: Mapping[str, Any], crawl: Crawl
) -> AsyncIterator[Listing]:
    total = 0
    for page in range(crawl.max_pages):
        listings = await _search_page(client, {**params, "page": page}, crawl)
        if not listings:
            return                      # a real page with no results: past the end
        total += len(listings)
        # One line per page. Without it a 40-page walk is ~4 minutes of silence
        # before the first fetch, which is indistinguishable from a hang --
        # discovery is collected in full before fetching starts.
        log.info("area=%s page %d/%d: %d listings (%d so far)",
                 params.get("area"), page + 1, crawl.max_pages, len(listings), total)
        for listing in listings:
            yield listing
    log.info(
        "stopped at max_pages=%d for area=%s; there may be more",
        crawl.max_pages,
        params.get("area"),
    )


async def _search_page(
    client: httpx2.AsyncClient, params: Mapping[str, Any], crawl: Crawl
) -> list[Listing]:
    """One page of results, or an empty list once past the last one.

    The whole reason this is its own function: **an exhausted search and a
    throttled request are both HTTP 200 with no vacancies on the page.**
    (Past hh's 40-page ceiling the answer is a 404 instead, handled below.)
    Ending the walk on "no results" would truncate the crawl at a random page
    whenever hh decided to throttle, and the run would still report success.
    A real results page always carries the search header; the throttling page
    -- a stripped "enable JavaScript" stub -- never does. So: header and no
    items means the end; no header means try again.
    """
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        await asyncio.sleep(crawl.delay * attempt)      # also the politeness delay
        resp = await client.get(SEARCH_URL, params=dict(params))
        # Verified 2026-09-24, re-checked 2026-09-29: hh serves pages 0-39 and
        # answers 404 from page 40 on -- and page 39 is a *full* 50, so the cap
        # is 2 000 results per query, not a page count that runs out.
        # That is the end of the results, not an error -- raising here
        # would abort the walk, and because `discover` is one generator across
        # every area, it would take the areas after this one with it.
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        # ~1.2MB of HTML: parsed off the event loop, or every other coroutine
        # waits for BeautifulSoup
        listings = await asyncio.to_thread(_results, resp.text)
        if listings is not None:
            return listings
        log.warning(
            "throttled on page %s (attempt %d/%d)", params.get("page"), attempt, SEARCH_ATTEMPTS
        )
    raise SearchThrottled(
        f"no search header after {SEARCH_ATTEMPTS} attempts at page {params.get('page')}"
    )


def _results(html: str) -> list[Listing] | None:
    """The page's listings, or None when it is the throttling stub."""
    soup = BeautifulSoup(html, "lxml")
    if soup.find(attrs={"data-qa": SEARCH_HEADER}) is None:
        return None
    return _listings(soup)


def _listings(soup: BeautifulSoup) -> list[Listing]:
    """Every result on the page -- from the embedded state, not the markup.

    Verified 2026-09-24, re-checked 2026-09-29: hh serves **50 results per page**
    but renders only the first 20 as ``data-qa`` anchors; the rest are drawn
    lazily, so reading the DOM silently takes 20 and calls the page done.
    Georgia is the clearest case -- one page, no paging block, and the walk
    stopped at 20 having seen a perfectly valid page (36 were there). The
    `<template>` blob is the same payload the page itself renders from, and it
    carries all 50 with ids, titles and links.

    The anchor scan stays as the fallback: it is what the saved test pages have,
    and it is the honest answer if hh ever drops the blob.
    """
    listings = _listings_from_state(soup)
    if listings:
        return listings
    return _listings_from_anchors(soup)


def _listings_from_state(soup: BeautifulSoup) -> list[Listing]:
    """Parse `vacancySearchResult.vacancies` out of the page's initial state."""
    template = soup.find("template")
    if template is None or not template.string:
        return []
    try:
        result = json.loads(template.string)["vacancySearchResult"]["vacancies"]
    except (ValueError, KeyError, TypeError):
        return []

    listings = []
    for item in result:
        link = (item.get("links") or {}).get("desktop")
        if not link:
            continue
        try:
            url = canonical_url(link)
            external_id = vacancy_id(url)
        except ValueError:
            continue
        listings.append(
            Listing(
                external_id=external_id,
                url=url,
                title=_clean(item.get("name") or "", inline=True),
            )
        )
    return listings


def _listings_from_anchors(soup: BeautifulSoup) -> list[Listing]:
    listings = []
    for anchor in soup.find_all("a", attrs={"data-qa": SEARCH_ITEM}, href=True):
        try:
            url = canonical_url(anchor["href"])
            external_id = vacancy_id(url)
        except ValueError:
            continue
        listings.append(
            Listing(
                external_id=external_id,
                url=url,
                title=_clean(anchor.get_text(" ", strip=True), inline=True),
            )
        )
    return listings


def _is_real_page(soup: BeautifulSoup) -> bool:
    """Is this an actual vacancy page, or hh's throttling stub?

    Under load hh answers **200 with a stripped page** — same trick it plays on
    search results — and that stub has no `data-qa` markers and no JSON-LD.
    Parsing it raises "No description found", which reads like a layout change
    and is nothing of the sort. A real page always has the title marker or the
    JobPosting block (branded layouts drop the markers but keep the JSON-LD).
    """
    return (
        soup.find(attrs={"data-qa": "vacancy-title"}) is not None
        or _json_ld(soup) is not None
    )


def _vacancy(html: str, url: str) -> VacancyData | None:
    """The parsed page, or None when it is the throttling stub."""
    if not _is_real_page(BeautifulSoup(html, "lxml")):
        return None
    return parse(html, url)


async def fetch(url: str) -> VacancyData:
    """One vacancy page, retried past the throttling stub.

    The retry is the same shape as `_search_page`'s: a stub is not a failure of
    the page, it is hh asking us to slow down, so back off and ask again rather
    than spending one of the vacancy's three attempts on it.
    """
    url = canonical_url(url)
    async with _client() as client:
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            resp = await client.get(url)
            if resp.status_code in (404, 410):
                raise VacancyGone(f"{resp.status_code} for {url}")
            resp.raise_for_status()
            data = await asyncio.to_thread(_vacancy, resp.text, url)   # ~700KB
            if data is not None:
                return data
            log.warning("throttled on %s (attempt %d/%d)", url, attempt, FETCH_ATTEMPTS)
            await asyncio.sleep(THROTTLE_BACKOFF * attempt)
    raise Throttled(f"only the stripped page after {FETCH_ATTEMPTS} attempts: {url}")


SOURCE = Source(
    name=NAME, hosts=HOSTS, discover=discover, fetch=fetch, crawl=CRAWL, rate=RATE
)
