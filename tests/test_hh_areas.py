"""hh's area tree and the limits it states for itself.

No network: `areas.json` is generated and checked in, and these read it. The
generator itself (`_refresh.py`) is the only thing here that would call hh, and
it is not exercised.
"""
import pytest

from jobmatch.sources import hh
from jobmatch.sources.hh import areas


def test_russia_is_its_regions_and_moscow_is_one_of_them():
    every = areas.regions_of("russia")
    assert len(every) == 89
    assert areas.MOSCOW in every


def test_moscow_is_the_only_region_split_further():
    """It exceeds the ceiling alone and has no child areas, so `experience` is
    the only way in. Nothing else in Russia needs it."""
    assert set(areas.OVERSIZED_REGIONS) == {areas.MOSCOW}
    split = [q for q in areas.coverage("russia") if "experience" in q]
    assert {q["area"] for q in split} == {areas.MOSCOW}


def test_the_generated_list_is_what_the_config_used_to_hold_by_hand():
    """The 88 ids were typed into config.toml until 2026-09-29. Regenerating must
    not quietly change what gets crawled."""
    covered = {q["area"] for q in areas.coverage("russia")}
    assert len(covered) == 89                      # 88 + Moscow, which is split
    assert 18563 in covered                        # the highest id in the old list
    assert 2 in covered                            # and the lowest


def test_areas_json_records_when_it_was_generated():
    assert areas.fetched().startswith("202")


def test_an_unknown_country_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="unknown country 'atlantis'"):
        areas.country_id("atlantis")


def test_country_names_are_case_and_space_insensitive():
    assert areas.country_id(" Georgia ") == areas.country_id("georgia") == 28


def test_a_small_country_is_one_query():
    """Belarus (~155) and Georgia (36) fit under the cap, so splitting them into
    regions would multiply requests for nothing."""
    assert areas.coverage("georgia") == [{"area": 28}]
    assert areas.coverage("belarus") == [{"area": 16}]


def test_russia_is_88_regions_plus_moscow_by_experience():
    queries = areas.coverage("russia")
    assert len(queries) == 92                       # 88 regions + 4 buckets
    moscow = [q for q in queries if q["area"] == areas.MOSCOW]
    assert [q["experience"] for q in moscow] == list(areas.EXPERIENCE_BUCKETS)
    plain = [q for q in queries if q["area"] != areas.MOSCOW]
    assert len(plain) == 88
    assert all(q.keys() == {"area"} for q in plain)  # nothing else differs


def test_coverage_of_an_unknown_country_is_refused():
    with pytest.raises(ValueError, match="unknown country"):
        areas.coverage("atlantis")


def test_every_named_country_is_coverable():
    """A name in COUNTRIES that areas.json cannot expand would be a silent
    empty crawl for whoever configured it."""
    for name in areas.COUNTRIES:
        assert areas.coverage(name)


def test_hh_states_the_limits_it_measured_for_itself():
    """These used to be config settings that every config had to restate -- and
    that a second config file then contradicted."""
    assert hh.CRAWL.max_pages == 40         # pages 0-39, then 404
    assert hh.CRAWL.max_pages * 50 == hh.RESULT_CEILING
    assert hh.RATE.workers == 3             # 6 made hh serve stripped pages
    assert hh.SOURCE.crawl is hh.CRAWL
    assert hh.SOURCE.rate is hh.RATE


def test_discover_falls_back_to_the_sources_own_crawl(monkeypatch):
    seen = []
    monkeypatch.setattr(hh, "_walk", lambda params, crawl: seen.append(crawl) or [])
    list(hh.discover({"text": "python", "area": [28]}))
    assert seen == [hh.CRAWL]


def test_discover_still_honours_an_explicit_crawl(monkeypatch):
    seen = []
    monkeypatch.setattr(hh, "_walk", lambda params, crawl: seen.append(crawl) or [])
    smoke = hh.Crawl(max_pages=2, delay=0.0)
    list(hh.discover({"text": "python", "area": [28]}, smoke))
    assert seen == [smoke]


def test_discover_expands_a_country_into_one_walk_per_query(monkeypatch):
    walked = []
    monkeypatch.setattr(hh, "_walk", lambda params, crawl: walked.append(params) or [])
    list(hh.discover({"text": "python"}, countries=["russia"]))
    assert len(walked) == 92
    assert sum("experience" in q for q in walked) == 4
    assert all(q["text"] == "python" for q in walked)        # search is carried
    assert all(q["order_by"] == hh.ORDER_BY for q in walked)  # and hh's ordering


def test_discover_applies_hh_ordering_but_lets_a_caller_override(monkeypatch):
    """Ordering decides *which* 2 000 results a capped query returns, so it is
    hh's -- but not immovably."""
    walked = []
    monkeypatch.setattr(hh, "_walk", lambda params, crawl: walked.append(params) or [])
    list(hh.discover({"text": "python"}, countries=["georgia"]))
    list(hh.discover({"text": "python", "order_by": "relevance"}, countries=["georgia"]))
    assert [q["order_by"] for q in walked] == [hh.ORDER_BY, "relevance"]


def test_discover_without_countries_still_walks_a_bare_area(monkeypatch):
    """The path the parser tests use: areas passed directly, no config involved."""
    walked = []
    monkeypatch.setattr(hh, "_walk", lambda params, crawl: walked.append(params.get("area")) or [])
    list(hh.discover({"text": "python", "area": [28, 16]}))
    assert walked == [28, 16]
