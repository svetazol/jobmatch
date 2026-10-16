"""Parsing is pure and offline: a handful of tags stand in for a 700KB page."""
import datetime as dt

import httpx2
import pytest

from jobmatch.sources import EmployerGone, hh

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

# Real English strings from hh vacancy pages (2026-10-16); the ids and company are illustrative.
PAGE_EN = """
<html><body>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Python developer",
 "datePosted": "2026-09-22T06:08:12.648+03:00", "identifier": {"value": "137615828"},
 "jobLocation": {"address": {"addressCountry": "GE"}}}
</script>
<h1 data-qa="vacancy-title">Python developer</h1>
<span data-qa="vacancy-company-name">\n  Тензор\n </span>
<div data-qa="vacancy-salary"><span data-qa="vacancy-salary-compensation-type-net">from <data value="80000">80\u00a0000</data> up to <data value="100000">100\u00a0000</data> <data value="RUB">₽</data> per month<span> <!-- -->after taxes</span></span></div>
<span data-qa="vacancy-experience">3-6 years</span>
<p data-qa="work-formats-text">Work format: <!-- -->at the employer's location, remotely or hybrid</p>
<div data-qa="vacancy-description"><p>First line</p><p>Second line</p></div>
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


@pytest.mark.parametrize("page, salary, experience", [
    (PAGE, "до 5 500 $ за месяц", "3–6 лет"),
    (PAGE_EN, "from 80 000 up to 100 000 ₽ per month after taxes", "3-6 years"),
], ids=["ru", "en"])
def test_both_languages_give_the_same_fields(page, salary, experience):
    """Rows fetched before the cookie stay Russian, so both must parse alike."""
    v = hh.parse(page, URL)

    assert (v.salary, v.experience) == (salary, experience)
    assert v.work_formats == ["onsite", "remote", "hybrid"]


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


def test_the_employer_id_comes_from_the_company_link():
    page = PAGE.replace(
        '<span data-qa="vacancy-company-name">',
        '<a data-qa="vacancy-company-name" href="/employer/1375441?hhtmFrom=vacancy">',
    ).replace("Тензор\n </span>", "Тензор\n </a>")

    v = hh.parse(page, URL)

    assert (v.company, v.employer_id) == ("Тензор", "1375441")


def test_an_anonymous_posting_has_no_employer_id():
    """The fixture's company name is a plain span: no link, no id."""
    assert hh.parse(PAGE, URL).employer_id is None


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


@pytest.mark.parametrize("phrase, expected", [
    ("travel", ["field_work"]),
    ("at the employer's location or hybrid", ["onsite", "hybrid"]),
    ("rotation or remotely", ["rotation", "remote"]),     # unknown English is kept too
])
def test_english_work_formats(phrase, expected):
    page = PAGE_EN.replace("at the employer's location, remotely or hybrid", phrase)

    assert hh.parse(page, URL).work_formats == expected


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


# --- employer pages ----------------------------------------------------------

EMPLOYER = """
<html><body>
<h1 data-qa="company-header-title"><span data-qa="company-header-title-name">Okko
  <span class="trusted-employer-check-icon">✓</span></span></h1>
<div data-qa="company-info-address"><span>Санкт-Петербург</span><span>Город</span></div>
<div data-qa="company-info-industries"><span>Информационные технологии</span><span>Сферы</span></div>
<a data-qa="sidebar-company-site" href="http://okko.tv/"><span>http://okko.tv/</span></a>
</body></html>
"""


def test_parses_an_employer_page():
    e = hh.parse_employer(EMPLOYER, "1375441")

    assert e.external_id == "1375441"
    assert e.url == "https://hh.ru/employer/1375441"
    assert e.name == "Okko"                  # not the trusted-employer badge after it
    assert e.site_url == "http://okko.tv/"
    assert e.raw == {"city": "Санкт-Петербург", "industries": "Информационные технологии"}


# Real strings, verbatim from employer 9111934 read in English (2026-10-16).
EMPLOYER_EN = """
<html><body>
<h1 data-qa="company-header-title"><span data-qa="company-header-title-name">ТРТ</span></h1>
<div data-qa="company-info-address"><div data-qa="cell"><span>Samara</span><span>City</span></div></div>
<div data-qa="company-info-category"><div data-qa="cell"><span>Recruitment agency</span><span>Registration type</span></div></div>
</body></html>
"""


def test_parses_an_employer_page_read_in_english():
    e = hh.parse_employer(EMPLOYER_EN, "9111934")

    assert e.raw == {"city": "Samara", "category": "Recruitment agency"}


def test_pages_are_asked_for_in_english():
    """A cookie, not a header: a literal Cookie header would shut out hh's own cookies."""
    request = hh._client().build_request("GET", URL)

    assert request.headers["cookie"] == "session_language=EN"


def test_an_employer_without_a_site_has_none():
    page = EMPLOYER.replace('data-qa="sidebar-company-site" href="http://okko.tv/"', "")

    assert hh.parse_employer(page, "1").site_url is None


def test_a_site_that_is_not_http_is_dropped():
    """The UI renders it as a link, and Vue would run a `javascript:` one."""
    page = EMPLOYER.replace("http://okko.tv/", "javascript:alert(1)")

    assert hh.parse_employer(page, "1").site_url is None


def test_the_employer_stub_is_none_and_a_changed_layout_raises():
    """The stub is retried; a real page without the name is not, or a layout
    change would cost 18 s of backoff and then be reported as throttling."""
    assert hh.parse_employer(STRIPPED, "1") is None
    with pytest.raises(hh.ParseError):
        hh.parse_employer('<html><body><div data-qa="something">x</div></body></html>', "1")


@pytest.mark.anyio
async def test_fetch_employer_retries_past_the_stub(monkeypatch, serve):
    get = FakeFetch(STRIPPED, EMPLOYER)
    serve(get)
    monkeypatch.setattr(hh, "THROTTLE_BACKOFF", 0.0)

    e = await hh.fetch_employer("1375441")

    assert (get.calls, e.name) == (2, "Okko")


@pytest.mark.anyio
async def test_a_missing_employer_raises_gone(serve):
    serve(lambda request: httpx2.Response(404, text=""))

    with pytest.raises(EmployerGone):
        await hh.fetch_employer("1")


# --- seniority: hh's experience bucket against the user's years --------------

@pytest.mark.parametrize("experience, years, expected", [
    ("не требуется", 5, "below"),
    ("1–3 года", 5, "below"),
    ("3–6 лет", 5, "level"),
    ("более 6 лет", 5, "above"),
    ("3–6 лет", 3, "level"),          # both ends are inside the bucket
    ("3–6 лет", 6, "level"),
    ("3–6 лет", 2, "above"),
    ("3–6 лет", 7, "below"),
    ("более 6 лет", 7, "level"),      # open-ended: no ceiling to be above
    ("не требуется", 0, "level"),
    ("not required", 5, "below"),     # English, since the session_language cookie
    ("1-3 years", 5, "below"),
    ("3-6 years", 5, "level"),
    ("more than 6 years", 5, "above"),
])
def test_seniority_places_the_bucket_against_the_years(experience, years, expected):
    assert hh.seniority_fit(experience, years) == expected


@pytest.mark.parametrize("experience, years", [
    ("", 5), (None, 5), ("3-6 лет", 5), ("от 2 лет", 5), ("3–6 лет", None),
])
def test_seniority_is_unknown_rather_than_guessed(experience, years):
    """A hyphen for the en-dash is unknown text too: the labels are hh's, verbatim."""
    assert hh.seniority_fit(experience, years) is None


@pytest.mark.parametrize("page", [PAGE, PAGE_EN], ids=["ru", "en"])
def test_the_parsed_experience_is_a_key_seniority_knows(page):
    """The parser's output and the mapping's keys must agree, or every hint is silently null."""
    assert hh.parse(page, URL).experience in hh.HH_EXPERIENCE_YEARS
