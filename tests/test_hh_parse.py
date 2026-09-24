"""Parsing is pure and offline: a handful of tags stand in for a 700KB page."""
import datetime as dt

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


def test_discover_makes_one_request_per_area(monkeypatch):
    """The 20-item cap is per request, so a list of areas must fan out."""
    seen = []

    class FakeResponse:
        text = "<rss><channel></channel></rss>"

        @staticmethod
        def raise_for_status():
            pass

    def fake_get(url, params, headers, timeout):
        seen.append(params)
        return FakeResponse()

    monkeypatch.setattr(hh.requests, "get", fake_get)
    monkeypatch.setattr(hh.time, "sleep", lambda _: None)

    list(hh.discover({"text": "python", "area": [113, 28, 1001]}))

    assert [p["area"] for p in seen] == [113, 28, 1001]
    assert all(p["text"] == "python" for p in seen)


def test_discover_still_works_with_a_single_area_or_none(monkeypatch):
    seen = []

    class FakeResponse:
        text = "<rss><channel></channel></rss>"

        @staticmethod
        def raise_for_status():
            pass

    monkeypatch.setattr(hh.requests, "get", lambda url, params, headers, timeout: (
        seen.append(params) or FakeResponse()
    ))

    list(hh.discover({"text": "python", "area": 28}))
    list(hh.discover({"text": "python"}))

    assert seen == [{"text": "python", "area": 28}, {"text": "python"}]
