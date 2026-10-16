"""hh.ru: search, vacancy and employer pages, parsed by ``data-qa`` with a JSON-LD fallback."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx2
from bs4 import BeautifulSoup

from .. import (
    Crawl, EmployerData, EmployerGone, Listing, Rate, Source, VacancyData, VacancyGone,
)
from . import areas

log = logging.getLogger(__name__)

NAME = "hh.ru"

# Measured 2026-09-24: 6 workers got stripped pages for 597 vacancies; 3 at 1.0s held.
RATE = Rate(workers=3, delay=1.0)

# Measured 2026-09-29: pages 0-39 of 50, 404 from page 40; pages are ~1.2MB, hence the delay.
CRAWL = Crawl(max_pages=40, delay=2.5)
RESULT_CEILING = 2000

# Coverage, not preference: newest-first is the only order a repeated crawl converges under.
ORDER_BY = "publication_time"
# HTML results, not RSS: RSS serves only the newest 20 per query.
SEARCH_URL = "https://hh.ru/search/vacancy"
# On every real results page; its absence gives a throttled response away.
SEARCH_HEADER = "vacancies-search-header"
SEARCH_ITEM = "serp-item__title"
SEARCH_ATTEMPTS = 3
FETCH_ATTEMPTS = 3       # past the throttling stub
THROTTLE_BACKOFF = 3.0   # seconds, times the attempt number
# One engine and one id space, so either domain gives one row; the country is `area`, not the domain.
HOSTS = ("hh.ru", "headhunter.ge")
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
# English pages, measured 2026-10-16; a cookie jar, not a Cookie header, so hh's own cookies still join it.
COOKIES = {"session_language": "EN"}

_VACANCY_ID = re.compile(r"/vacancy/(\d+)")
# An anonymous posting has the company name without this link.
_EMPLOYER_ID = re.compile(r"/employer/(\d+)")
EMPLOYER_URL = "https://hh.ru/employer/{}"
# The site's own wording; an unknown phrase is kept verbatim so it shows up rather than vanishing.
_WORK_FORMAT_PREFIX = re.compile(r"^(?:Формат работы|Work format):\s*")
_WORK_FORMAT_SPLIT = re.compile(r",\s*|\s+(?:или|or)\s+")
# JSON-LD's ISO code -> English; an unmapped country stays a bare code, visibly rather than wrongly.
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
    # English, since the session_language cookie (measured 2026-10-16); rows before it stay Russian.
    "at the employer's location": "onsite",
    "remotely": "remote",
    "hybrid": "hybrid",
    "travel": "field_work",
}

# hh's four buckets as stored in `experience_raw`, in years; measured 2026-10-15: 3037/2537/658/510, 40 empty.
HH_EXPERIENCE_YEARS: dict[str, tuple[int, int | None]] = {
    "не требуется": (0, 0),
    "1–3 года": (1, 3),
    "3–6 лет": (3, 6),
    "более 6 лет": (6, None),
    # English, since the session_language cookie (measured 2026-10-16); rows before it stay Russian.
    "not required": (0, 0),
    "1-3 years": (1, 3),
    "3-6 years": (3, 6),
    "more than 6 years": (6, None),
}

type Seniority = Literal["below", "level", "above"]

_SPACES = re.compile(r"[^\S\n]+")
_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")


class ParseError(RuntimeError):
    """The page loaded but doesn't look like a vacancy any more."""


def _client() -> httpx2.AsyncClient:
    """A function, not a module client: a client belongs to its event loop, and tests swap it."""
    return httpx2.AsyncClient(
        headers=HEADERS, cookies=COOKIES, timeout=TIMEOUT, follow_redirects=True
    )


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
    """Collapse nbsp and whitespace runs; ``inline=True`` folds newlines too, for one-line fields."""
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
    value = _WORK_FORMAT_PREFIX.sub("", value)
    return [
        WORK_FORMATS.get(part.lower(), part.lower())
        for raw_part in _WORK_FORMAT_SPLIT.split(value)
        if (part := raw_part.strip())
    ]


def seniority_fit(experience_raw: str | None, years: int | None) -> Seniority | None:
    """Where the posting sits against the user's years: "below" asks for less than they have."""
    bucket = HH_EXPERIENCE_YEARS.get(experience_raw or "")
    if bucket is None or years is None:
        return None
    low, high = bucket
    if years < low:
        return "above"
    if high is not None and years > high:
        return "below"
    return "level"


def _country(ld: dict) -> str | None:
    """From the posting's ISO code, not the searched area: "other regions" returns any country."""
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

    # Branded layouts drop the markers; JSON-LD is the fallback.
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

    company_link = soup.find(attrs={"data-qa": "vacancy-company-name"}, href=True)
    employer = _EMPLOYER_ID.search(company_link["href"]) if company_link else None

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
        employer_id=employer.group(1) if employer else None,
        raw={"json_ld": ld} if ld else {},
    )


def _info(soup: BeautifulSoup, qa: str) -> str | None:
    """A `company-info-*` block: the value first, then its label ("Город" or "City")."""
    el = soup.find(attrs={"data-qa": qa})
    first = next(iter(el.stripped_strings), None) if el else None
    return _clean(first, inline=True)


def _web_url(href: str | None) -> str | None:
    """http(s) only: the UI renders it as a link, and Vue does not block `javascript:`."""
    if href and urlsplit(href).scheme in ("http", "https"):
        return href
    return None


def parse_employer(html: str, employer_id: str) -> EmployerData | None:
    """None for the stub (no `data-qa` at all). Markers verified 2026-10-07 on Okko, Яндекс, СБЕР."""
    soup = BeautifulSoup(html, "lxml")
    if soup.find(attrs={"data-qa": True}) is None:
        return None
    name_el = soup.find(attrs={"data-qa": "company-header-title-name"})
    if name_el is None:
        raise ParseError(
            f"No employer name on employer {employer_id}; the layout may have changed"
        )
    site = soup.find(attrs={"data-qa": "sidebar-company-site"}, href=True)
    raw = {
        key: value
        for key, qa in (
            ("city", "company-info-address"),
            ("industries", "company-info-industries"),
            ("category", "company-info-category"),
        )
        if (value := _info(soup, qa))
    }
    return EmployerData(
        external_id=employer_id,
        url=EMPLOYER_URL.format(employer_id),
        # The first string only: a trusted employer's name is followed by a badge.
        name=_clean(next(iter(name_el.stripped_strings), None), inline=True),
        site_url=_web_url(site["href"] if site else None),
        raw=raw,
    )


class Throttled(RuntimeError):
    """hh kept serving its stripped 200 page instead of content: slow down."""


# The search path's older name.
SearchThrottled = Throttled


async def discover(
    params: Mapping[str, Any],
    crawl: Crawl | None = None,
    countries: Sequence[str] = (),
) -> AsyncIterator[Listing]:
    """Walk each country's queries (else ``params``' areas), yielding a vacancy once as queries overlap."""
    crawl = crawl or CRAWL
    base: dict[str, Any] = {"order_by": ORDER_BY, **dict(params)}
    if countries:
        queries = [query for country in countries for query in areas.coverage(country)]
    else:
        spec = base.pop("area", None)
        listed = spec if isinstance(spec, (list, tuple)) else [spec]
        queries = [{} if area is None else {"area": area} for area in listed]

    seen: set[str] = set()
    async with _client() as client:
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
        # Otherwise a 40-page walk is ~4 minutes of silence that looks like a hang.
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
    """Exhausted and throttled are both an empty 200; only the search header tells end from retry."""
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        await asyncio.sleep(crawl.delay * attempt)      # also the politeness delay
        resp = await client.get(SEARCH_URL, params=dict(params))
        # The end, not an error (404 from page 40, 2026-09-29): raising would abort every later area.
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        # ~1.2MB, so off the event loop.
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
    """From the embedded state, not the markup: measured 2026-09-29, 50 per page but 20 anchors."""
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
    """The throttling stub has neither the title marker nor JSON-LD; a real page has one."""
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
    """Retried past the stub, which must not spend one of the vacancy's fetch attempts."""
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


async def fetch_employer(employer_id: str) -> EmployerData:
    """Retried past the stub like `fetch`; on demand, one at a time, so it needs no rate."""
    url = EMPLOYER_URL.format(employer_id)
    async with _client() as client:
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            resp = await client.get(url)
            if resp.status_code == 404:
                raise EmployerGone(f"404 for {url}")
            resp.raise_for_status()
            data = await asyncio.to_thread(parse_employer, resp.text, employer_id)
            if data is not None:
                return data
            log.warning("throttled on %s (attempt %d/%d)", url, attempt, FETCH_ATTEMPTS)
            await asyncio.sleep(THROTTLE_BACKOFF * attempt)
    raise Throttled(f"only the stripped page after {FETCH_ATTEMPTS} attempts: {url}")


SOURCE = Source(
    name=NAME, hosts=HOSTS, discover=discover, fetch=fetch, crawl=CRAWL, rate=RATE,
    fetch_employer=fetch_employer,
)
