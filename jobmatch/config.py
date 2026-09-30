"""config.toml -> a frozen Settings. Secrets are in .env.

Choices only: what to search for, where, and with which CV. hh's ceiling, fetch
rate, ordering and area coverage live in ``sources/hh`` -- the line is whether
the site itself determines the value. A setting a config must restate is a
setting a config can contradict.
"""
from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sources import SOURCES, Crawl, Rate

DEFAULT_PATH = Path("config.toml")

# Keys allowed inside a [sources."<name>"] table.
_SOURCE_KEYS = {"search", "sweeps"}


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One site, its search, and the sweeps it is crawled in.

    ``search`` is opaque: passed to the source verbatim. Nested under the source
    because each site has its own query vocabulary -- and so two sweeps cannot
    drift apart.
    """

    name: str
    search: Mapping[str, Any]
    sweeps: Mapping[str, tuple[str, ...]]   # name -> country names


@dataclass(frozen=True, slots=True)
class Settings:
    sources: tuple[SourceConfig, ...]
    # None means "use the source's own measured value" (sources/hh RATE, CRAWL).
    # Overrides for experiments; not needed for a correct config.
    fetch_delay: float | None = None
    fetch_workers: int | None = None
    crawl: Crawl | None = None
    model: str = "jev-1.13"          # pinned; part of the match fingerprint
    cv_path: Path = Path("data/cv.md")
    # A hash, not a path: answers are stored under the hash of the CV that
    # earned them, which survives the file being edited or renamed. Empty means
    # every CV at once -- honest only while there has been one.
    stats_cv_hash: str = ""

    def rate_for(self, source_rate: Rate) -> Rate:
        """The source's measured rate, with any config override applied."""
        return Rate(
            workers=max(1, self.fetch_workers if self.fetch_workers is not None
                        else source_rate.workers),
            delay=self.fetch_delay if self.fetch_delay is not None else source_rate.delay,
        )

    def crawls(self) -> tuple[tuple[SourceConfig, str, tuple[str, ...]], ...]:
        """Every (source, sweep name, countries) this run should walk."""
        return tuple(
            (source, sweep, countries)
            for source in self.sources
            for sweep, countries in source.sweeps.items()
        )

    def only(self, sweep: str) -> Settings:
        """Narrowed to one sweep, across every source that defines it."""
        narrowed = tuple(
            dataclasses.replace(source, sweeps={sweep: source.sweeps[sweep]})
            for source in self.sources
            if sweep in source.sweeps
        )
        if not narrowed:
            known = sorted({s for source in self.sources for s in source.sweeps})
            raise ValueError(
                f"no sweep named {sweep!r}; sweeps in this config: "
                f"{', '.join(known) or '(none)'}"
            )
        return dataclasses.replace(self, sources=narrowed)


def load_settings(path: Path | str = DEFAULT_PATH) -> Settings:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"No config at {path}; copy the one in the repo root") from None

    # A misspelled key is silently ignored otherwise: `fetch_worker` (no s) ran
    # one worker and made the crawl three times slower without a word.
    known = {field.name for field in dataclasses.fields(Settings)}
    if unknown := sorted(set(raw) - known):
        raise ValueError(
            f"{path}: unknown setting(s) {', '.join(unknown)}; "
            f"known settings are {', '.join(sorted(known))}"
        )

    sources = _sources(path, raw.pop("sources", {}))
    crawl = raw.get("crawl")

    return Settings(
        sources=sources,
        fetch_delay=None if "fetch_delay" not in raw else float(raw["fetch_delay"]),
        fetch_workers=(
            None if "fetch_workers" not in raw else max(1, int(raw["fetch_workers"]))
        ),
        crawl=None if crawl is None else Crawl(
            max_pages=int(crawl.get("max_pages", 40)),
            delay=float(crawl.get("delay", 2.5)),
        ),
        model=str(raw.get("model", "jev-1.13")),
        cv_path=Path(raw.get("cv_path", "data/cv.md")),
        stats_cv_hash=str(raw.get("stats_cv_hash", "")).strip(),
    )


def _sources(path: Path, table: Any) -> tuple[SourceConfig, ...]:
    """``[sources."hh.ru".search]`` / ``.sweeps`` -> SourceConfig.

    Fails loudly: a config that names a source, sweep or country wrongly must
    say so before the first request, not crawl nothing and report success.
    """
    if not isinstance(table, dict) or not table:
        raise ValueError(f"{path} enables no sources; expected [sources.\"<name>\"]")
    if unknown := sorted(set(table) - set(SOURCES)):
        raise ValueError(
            f"{path} names unknown source(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(SOURCES))}"
        )

    configured = []
    for name, entry in table.items():
        if stray := sorted(set(entry) - _SOURCE_KEYS):
            raise ValueError(
                f"{path}: [sources.\"{name}\"] has unknown key(s) "
                f"{', '.join(stray)}; expected {' and '.join(sorted(_SOURCE_KEYS))}"
            )
        sweeps = entry.get("sweeps") or {}
        if not sweeps:
            raise ValueError(f"{path}: [sources.\"{name}\".sweeps] defines no sweeps")
        configured.append(
            SourceConfig(
                name=name,
                search=entry.get("search", {}),
                sweeps={
                    sweep: _countries(path, name, sweep, countries)
                    for sweep, countries in sweeps.items()
                },
            )
        )
    return tuple(configured)


def _countries(path: Path, source: str, sweep: str, countries: Any) -> tuple[str, ...]:
    """Validated at load, so a typo costs nothing.

    ``areas`` is imported here because the vocabulary is hh's specifically; with
    a second source the check moves onto the source itself.
    """
    from .sources.hh import areas

    if isinstance(countries, str) or not countries:
        raise ValueError(
            f'{path}: sweep "{sweep}" of {source} must be a non-empty list of '
            f"country names, got {countries!r}"
        )
    names = tuple(str(country) for country in countries)
    for name in names:
        areas.country_id(name)      # raises, naming the countries it knows
    return names
