"""hh's area tree (``areas.json``, from ``_refresh.py``), and the queries that cover a country."""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

AREAS_PATH = Path(__file__).with_name("areas.json")

# Closed, so a typo is an error rather than a silent empty crawl.
COUNTRIES = {
    "russia": 113,
    "ukraine": 5,
    "kazakhstan": 40,
    "azerbaijan": 9,
    "belarus": 16,
    "georgia": 28,
    "kyrgyzstan": 48,
    "uzbekistan": 97,
    "other": 1001,      # hh's catch-all; returns whichever country the job is in
}

MOSCOW = 1

# Over RESULT_CEILING, so split by region. Measured 2026-09-29: Russia ~4 350, the rest fit one query.
OVERSIZED_COUNTRIES = frozenset({"russia"})

# The only facet whose buckets sum to the area's total; `schedule` overlaps.
EXPERIENCE_BUCKETS = (
    "noExperience", "between1And3", "between3And6", "moreThan6",
)

# Over the cap with no child areas. Measured 2026-09-29: Moscow ~2 716, buckets 113/~864/1 427/320.
OVERSIZED_REGIONS = {MOSCOW: EXPERIENCE_BUCKETS}


@cache
def _tree() -> dict[int, dict]:
    raw = json.loads(AREAS_PATH.read_text(encoding="utf-8"))
    return {country["id"]: country for country in raw["countries"]}


@cache
def fetched() -> str:
    """The date areas.json was generated."""
    return json.loads(AREAS_PATH.read_text(encoding="utf-8"))["fetched"]


def country_id(name: str) -> int:
    try:
        return COUNTRIES[name.strip().lower()]
    except KeyError:
        raise ValueError(
            f"unknown country {name!r}; known: {', '.join(sorted(COUNTRIES))}"
        ) from None


def regions_of(country: str | int) -> list[int]:
    """Every child area of ``country``; ``coverage`` decides what to do with them."""
    area = country if isinstance(country, int) else country_id(country)
    tree = _tree()
    if area not in tree:
        raise ValueError(f"no area {area} in {AREAS_PATH.name}; regenerate it")
    return [r["id"] for r in tree[area]["regions"]]


def coverage(country: str) -> list[dict[str, Any]]:
    """Queries reaching every posting under hh's cap, each merged over the configured search."""
    name = country.strip().lower()
    area = country_id(name)
    if name not in OVERSIZED_COUNTRIES:
        return [{"area": area}]

    queries: list[dict[str, Any]] = []
    for region in regions_of(area):
        buckets = OVERSIZED_REGIONS.get(region)
        if buckets is None:
            queries.append({"area": region})
        else:
            queries += [{"area": region, "experience": b} for b in buckets]
    return queries


def main(argv: list[str] | None = None) -> int:
    """The queries a country name expands to."""
    import sys

    # Local: the package imports this module.
    from . import ORDER_BY, SEARCH_URL

    args = argv if argv is not None else sys.argv[1:]
    print(f"areas.json generated {fetched()}")
    print(f"GET {SEARCH_URL}")
    print(f"    order_by={ORDER_BY}, the configured search, and one line below each")
    if not args:
        for name, area in sorted(COUNTRIES.items(), key=lambda kv: kv[1]):
            note = " (over the cap: split by region)" if name in OVERSIZED_COUNTRIES else ""
            print(f"  {area:>5} {name:<12} {len(regions_of(area)):>3} regions{note}")
        return 0
    for name in args:
        queries = coverage(name)
        print(f"\n{name} -> {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}")
        for query in queries:
            print("  " + "&".join(f"{k}={v}" for k, v in query.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
