"""Parsing is pure and offline: a handful of tags stand in for a 700KB page."""
import datetime as dt

import pytest

from jobmatch.sources import hh

URL = "https://hh.ru/vacancy/137615828"

PAGE = """
<html><body>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Python developer",
 "datePosted": "2026-09-22T06:08:12.648+03:00", "identifier": {"value": "137615828"}}
</script>
<h1 data-qa="vacancy-title">Python developer</h1>
<span data-qa="vacancy-company-name">\n  Тензор\n </span>
<div data-qa="vacancy-salary">до\n5 500\n$\nза месяц</div>
<span data-qa="vacancy-experience">3–6 лет</span>
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
