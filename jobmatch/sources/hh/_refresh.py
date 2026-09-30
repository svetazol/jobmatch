"""Regenerate ``areas.json`` from hh's area tree.

    python -m jobmatch.sources.hh._refresh

Run it when hh adds or renames an area. Separate from ``areas.py`` so that
importing the source never reaches the network.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import requests

from . import HEADERS, TIMEOUT
from .areas import AREAS_PATH

API = "https://api.hh.ru/areas"


def fetch() -> dict:
    """The nine top-level areas and their children, flattened to what we use."""
    resp = requests.get(API, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return {
        "fetched": dt.date.today().isoformat(),
        "source": API,
        "countries": [
            {
                "id": int(country["id"]),
                "name": country["name"],
                "regions": [
                    {"id": int(region["id"]), "name": region["name"]}
                    for region in country["areas"]
                ],
            }
            for country in resp.json()
        ],
    }


def main() -> int:
    data = fetch()
    AREAS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"{AREAS_PATH}: {data['fetched']}")
    for country in data["countries"]:
        print(f"  {country['id']:>5} {country['name']:<22} {len(country['regions'])} regions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
