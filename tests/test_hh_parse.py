"""Parsing is pure and offline: a handful of tags stand in for a 700KB page."""
import datetime as dt

import httpx2
import pytest

from jobmatch.sources import hh

URL = "https://hh.ru/vacancy/137615828"

PAGE = """
<html><body>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Python developer",
 "datePosted": "2026-09-22T06:08:12.648+03:00", "identifier": {"value": "137615828"},
 "applicantLocationRequirements": {"@type": "Country", "name": "\u0413\u0440\u0443\u0437\u0438\u044f"},
 "jobLocation": {"address": {"addressCountry": "GE", "addressLocality": "\u0422\u0431\u0438\u043b\u0438\u0441\u0438"}}}
</script>
<h1 data-qa="vacancy-title">Python developer</h1>
<span data-qa="vacancy-company-name">\n  Тензор\n </span>
<div data-qa="vacancy-salary">до\n5 500\n$\nза месяц</div>
<span data-qa="vacancy-experience">3–6 лет</span>
<p data-qa="work-formats-text">Формат работы: на месте работодателя, удалённо или гибрид</p>
<div data-qa="vacancy-description"><p>First line</p><p>Second line</p></div>
<span data-qa="skills-element">Python</span>
<span data-qa="skills-element-2">SQL</span>
</body></html>
"""

BRANDED = """
<html><body>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Branded role",
 "hiringOrganization": {"name": "Acme"},
 "description": "<p>Only in JSON-LD</p>"}
</script>
</body></html>
"""


def test_parses_the_normal_layout():
    v = hh.parse(PAGE, URL)

    assert v.external_id == "137615828"
    assert v.title == "Python developer"
    assert v.company == "Тензор"
    assert v.salary == "до 5 500 $ за месяц"  # no \n or \xa0 debris
    assert v.experience == "3–6 лет"
    assert v.description == "First line\nSecond line"
    assert v.skills == ["Python", "SQL"]
    assert v.published_at == dt.datetime(
        2026, 9, 22, 6, 8, 12, 648000, tzinfo=dt.timezone(dt.timedelta(hours=3))
    )
    assert v.country == "Georgia"   # from the ISO code, not the searched area
    assert v.work_formats == ["onsite", "remote", "hybrid"]   # a set, not one value
    assert v.raw["json_ld"]["@type"] == "JobPosting"


def test_falls_back_to_json_ld_on_branded_pages():
    v = hh.parse(BRANDED, URL)

    assert (v.title, v.company, v.description) == ("Branded role", "Acme", "Only in JSON-LD")


def test_raises_when_the_layout_stops_matching():
    with pytest.raises(hh.ParseError):
        hh.parse("<html><body>captcha</body></html>", URL)


def test_url_is_canonicalised_and_carries_the_id():
    assert hh.canonical_url(URL + "?from=vacancy_search_list&hhtmFrom=x") == URL
    assert hh.vacancy_id(URL) == "137615828"
    with pytest.raises(ValueError):
        hh.vacancy_id("https://hh.ru/employer/123")


def test_an_unmapped_country_keeps_its_iso_code():
    """The "other regions" area can return any country; an unmapped one stays
    visible as its code rather than being silently dropped."""
    page = PAGE.replace('"addressCountry": "GE"', '"addressCountry": "RS"')

    assert hh.parse(page, URL).country == "RS"


def test_no_country_when_the_posting_has_no_iso_code():
    page = PAGE.replace('"addressCountry": "GE", ', "")

    assert hh.parse(page, URL).country is None


def test_an_unknown_work_format_is_kept_rather_than_dropped():
    """A new phrase should show up in the data, not vanish silently."""
    page = PAGE.replace(
        "на месте работодателя, удалённо или гибрид",
        "вахта или удалённо",
    )

    assert hh.parse(page, URL).work_formats == ["вахта", "remote"]


def test_a_posting_that_says_nothing_about_work_format():
    page = PAGE.replace('data-qa="work-formats-text"', 'data-qa="something-else"')

    assert hh.parse(page, URL).work_formats == []


# --- walking the search results -------------------------------------------

SEARCH_PAGE = """
<html><body>
<h1 data-qa="vacancies-search-header">Найдено 193 вакансии «python»</h1>
<a data-qa="serp-item__title" href="https://hh.ru/vacancy/1?from=serp">First job</a>
<a data-qa="serp-item__title" href="https://hh.ru/vacancy/2">Second job</a>
<a data-qa="serp-item__title" href="/employer/99">Not a vacancy</a>
</body></html>
"""

# A real page past the last result: header present, no vacancies.
SEARCH_END = """
<html><body>
<h1 data-qa="vacancies-search-header">Найдено 193 вакансии «python»</h1>
</body></html>
"""

# What hh serves when it throttles: HTTP 200, no header, no results.
SEARCH_THROTTLED = """
<html><body>
Для работы с нашим сайтом необходимо, чтобы Вы включили JavaScript.
</body></html>
"""

FAST = hh.Crawl(max_pages=5, delay=0.0)


@pytest.fixture
def serve(monkeypatch):
    """Point hh's HTTP client at a handler instead of the network."""
    def install(handler):
        monkeypatch.setattr(
            hh, "_client", lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        )
        return handler
    return install


async def walk(*args, **kwargs):
    return [listing async for listing in hh.discover(*args, **kwargs)]


class FakeGet:
    """Serves a scripted page per request and records the params it saw."""

    def __init__(self, *pages, not_found_from=None):
        self.pages = list(pages)
        self.seen = []
        # hh answers 404 past its 40-page ceiling; `not_found_from` is the
        # page index where this fake starts doing the same.
        self.not_found_from = not_found_from

    def __call__(self, request):
        # query values arrive as strings; numbers are compared as numbers
        params = {k: int(v) if v.isdigit() else v for k, v in request.url.params.items()}
        self.seen.append(params)
        page = params.get("page", 0)
        if self.not_found_from is not None and page >= self.not_found_from:
            return httpx2.Response(404, text="")
        return httpx2.Response(200, text=self.pages.pop(0) if self.pages else SEARCH_END)


@pytest.mark.anyio
async def test_a_page_of_results_becomes_listings(serve):
    serve(FakeGet(SEARCH_PAGE, SEARCH_END))

    listings = await walk({"text": "python"}, FAST)

    assert [l.external_id for l in listings] == ["1", "2"]     # the employer link is skipped
    assert listings[0].url == "https://hh.ru/vacancy/1"        # tracking params stripped
    assert listings[1].title == "Second job"                   # nbsp normalised


@pytest.mark.anyio
async def test_the_walk_stops_at_a_real_empty_page(serve):
    get = FakeGet(SEARCH_PAGE, SEARCH_PAGE, SEARCH_END, SEARCH_PAGE)
    serve(get)

    await walk({"text": "python"}, FAST)

    assert [p["page"] for p in get.seen] == [0, 1, 2]  # never asked for page 3


@pytest.mark.anyio
async def test_a_throttled_page_is_retried_not_mistaken_for_the_end(serve):
    """The bug this guards: both look like HTTP 200 with no vacancies."""
    get = FakeGet(SEARCH_PAGE, SEARCH_THROTTLED, SEARCH_PAGE, SEARCH_END)
    serve(get)

    listings = await walk({"text": "python"}, FAST)

    assert len(get.seen) == 4                       # the throttled page 1 was re-requested
    assert [p["page"] for p in get.seen] == [0, 1, 1, 2]
    assert len(listings) == 2                       # same two ids, deduplicated


@pytest.mark.anyio
async def test_persistent_throttling_raises_rather_than_truncating(serve):
    serve(FakeGet(SEARCH_PAGE, *[SEARCH_THROTTLED] * hh.SEARCH_ATTEMPTS))

    with pytest.raises(hh.SearchThrottled):
        await walk({"text": "python"}, FAST)


@pytest.mark.anyio
async def test_max_pages_caps_the_walk(serve):
    get = FakeGet(*[SEARCH_PAGE] * 10)
    serve(get)

    await walk({"text": "python"}, hh.Crawl(max_pages=3, delay=0.0))

    assert [p["page"] for p in get.seen] == [0, 1, 2]


@pytest.mark.anyio
async def test_each_area_is_walked_separately_and_ids_are_not_repeated(serve):
    get = FakeGet(SEARCH_PAGE, SEARCH_END, SEARCH_PAGE, SEARCH_END)
    serve(get)

    listings = await walk({"text": "python", "area": [28, 16]}, FAST)

    assert [p.get("area") for p in get.seen] == [28, 28, 16, 16]
    assert [l.external_id for l in listings] == ["1", "2"]  # both areas returned the same two


@pytest.mark.anyio
async def test_a_404_ends_the_walk_instead_of_raising(serve):
    """hh answers 404 from page 40 on — the end of the results, not an error.

    Raising there would abort the walk, and since `discover` is one generator
    across every configured area, it would take the remaining areas with it.
    """
    get = FakeGet(SEARCH_PAGE, SEARCH_PAGE, not_found_from=2)
    serve(get)

    listings = await walk({"text": "python", "area": [1, 2]}, FAST)

    # area 1 walked two full pages then met the 404 and stopped there;
    # crucially area 2 was still reached, which a raised error would have
    # prevented
    assert [p["area"] for p in get.seen] == [1, 1, 1, 2]
    assert [p["page"] for p in get.seen] == [0, 1, 2, 0]
    assert len(listings) == 2          # the two ids, deduped across both areas


STRIPPED = """<!doctype html><html><body>
<noscript>Для работы с нашим сайтом необходимо, чтобы Вы включили JavaScript</noscript>
</body></html>"""


class FakeFetch:
    """Serves scripted bodies for fetch(), recording how many times it was hit."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return httpx2.Response(200, text=self.bodies.pop(0) if self.bodies else STRIPPED)


@pytest.mark.anyio
async def test_the_throttling_stub_is_retried_not_reported_as_a_layout_change(monkeypatch, serve):
    """hh answers 200 with a stripped page when we push too hard.

    Parsing it raises "No description found", which reads like the site
    changed and is nothing of the sort — and worse, it burns one of the
    vacancy's three fetch attempts on a problem that is ours, not the page's.
    """
    get = FakeFetch(STRIPPED, STRIPPED, PAGE)
    serve(get)
    monkeypatch.setattr(hh, "THROTTLE_BACKOFF", 0.0)

    data = await hh.fetch("https://hh.ru/vacancy/1")

    assert get.calls == 3, "should have retried past both stubs"
    assert data.description, "the third, real page should have parsed"


@pytest.mark.anyio
async def test_a_page_that_is_only_ever_stripped_raises_throttled(monkeypatch, serve):
    """Distinct from ParseError: this one says 'slow down', not 'page changed'."""
    get = FakeFetch()          # every body is the stub
    serve(get)
    monkeypatch.setattr(hh, "THROTTLE_BACKOFF", 0.0)

    with pytest.raises(hh.Throttled):
        await hh.fetch("https://hh.ru/vacancy/1")
    assert get.calls == hh.FETCH_ATTEMPTS
