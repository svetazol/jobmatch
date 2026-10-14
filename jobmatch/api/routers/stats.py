"""The market view: aggregates over one CV's matches."""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ... import matching, repository
from ...config import DEFAULT_PATH, Settings, load_settings
from ..deps import get_session
from ..schemas import CvOption, NameCount, PitchStat, Search, Stats

router = APIRouter(prefix="/api/stats", tags=["stats"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# The ladder in order; never sort it alphabetically.
FIT_LEVELS = ("poor", "weak", "good", "strong", "excellent")

# hh's search field names, as a person would say them.
_FIELD_NAMES = {"name": "title", "description": "description",
                "company_name": "company", "skills": "skills"}


def _split_terms(raw: str) -> list[str]:
    """hh's comma-separated excluded_text, its exact-match quotes removed for display."""
    return [term.strip().strip('"').strip() for term in raw.split(",") if term.strip()]


def _search() -> Search:
    """The one place that looks inside a source's opaque search: to describe it, never to act on it."""
    empty = Search(source="", keyword="", fields=[], excluded=[])
    try:
        settings = load_settings(DEFAULT_PATH)
    except (FileNotFoundError, ValueError, OSError):
        return empty

    for source in settings.sources:
        keyword = source.search.get("text")
        if not keyword:
            continue
        fields = source.search.get("search_field") or []
        if isinstance(fields, str):
            fields = [fields]
        return Search(
            source=source.name,
            keyword=str(keyword),
            fields=[_FIELD_NAMES.get(f, f) for f in fields],
            excluded=_split_terms(str(source.search.get("excluded_text") or "")),
        )
    return empty


def _settings() -> Settings | None:
    """None if the config cannot be read, so the view shows everything rather than nothing."""
    try:
        return load_settings(DEFAULT_PATH)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _cv_hash() -> str | None:
    """The default CV; empty means every CV, honest only while there has been one."""
    settings = _settings()
    return (settings.stats_cv_hash or None) if settings else None


# Keyed on what an edit changes, so an edited CV is hashed again.
_names: dict[tuple[str, int, int], str] = {}


def _disk_names(settings: Settings | None) -> dict[str, str]:
    """hash -> file name beside the configured CV; a non-CV file matches no row, so it is harmless."""
    if settings is None:
        return {}
    found: dict[str, str] = {}
    try:
        candidates = sorted(settings.cv_path.parent.glob("*.md"))
    except OSError:
        return {}
    for path in candidates:
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size)
            digest = _names.get(key)
            if digest is None:
                digest = _names[key] = matching.sha256(matching.load_cv(path))
        except (OSError, ValueError):
            continue          # unreadable or unsanitisable: it names nothing
        found.setdefault(digest, path.name)
    return found


async def _catalogue(session: AsyncSession, settings: Settings | None) -> list[CvOption]:
    """Driven by the database, named from the disk, so a deleted file's answers are still listed."""
    names = await asyncio.to_thread(_disk_names, settings)
    return [
        CvOption(
            name=names.get(cv_hash, cv_hash[:12]),
            cv_hash=cv_hash,
            n=count,
            last_matched=last,
            on_disk=cv_hash in names,
        )
        for cv_hash, count, last in await repository.cv_hashes(session)
    ]


def _select(catalogue: list[CvOption], requested: str | None,
            default_hash: str | None) -> CvOption | None:
    """An unknown CV is a 404: the average of all CVs would look like a correct answer."""
    if requested:
        for option in catalogue:
            if requested in (option.name, option.cv_hash):
                return option
        raise HTTPException(
            404,
            f"no matches stored for CV {requested!r}; "
            f"have {', '.join(o.name for o in catalogue) or 'none'}",
        )
    if default_hash:
        return next((o for o in catalogue if o.cv_hash == default_hash), None)
    return None


@router.get("", response_model=Stats)
async def get_stats(
    session: SessionDep,
    country: Annotated[list[str] | None, Query()] = None,
    cv: Annotated[str | None, Query(description="CV file name, or its hash")] = None,
) -> Stats:
    """For one CV (`?cv=`, else `stats_cv_hash`); disk reads run in a thread, off the event loop."""
    settings = await asyncio.to_thread(_settings)
    catalogue = await _catalogue(session, settings)
    selected = _select(catalogue, cv, settings.stats_cv_hash if settings else None)
    raw = await repository.stats(
        session, country=country, cv_hash=selected.cv_hash if selected else None
    )
    total, qualified, worth, median, mean = raw["headline"]

    fit_distribution = {level: 0 for level in FIT_LEVELS}
    for label, count in raw["fit_rows"]:
        # An unexpected label is shown, never dropped; see docs/architecture.md §10.
        fit_distribution[label] = fit_distribution.get(label, 0) + count

    pitches: dict[str, dict] = {}
    for angle, label, count, mean_fit, qual in raw["pitch_rows"]:
        entry = pitches.setdefault(
            angle, {"n": 0, "qualified": 0, "weighted": 0.0,
                    "distribution": dict.fromkeys(FIT_LEVELS, 0)},
        )
        entry["n"] += count
        entry["qualified"] += qual
        entry["weighted"] += float(mean_fit or 0) * count
        entry["distribution"][label] = entry["distribution"].get(label, 0) + count

    pitch_stats = [
        PitchStat(
            pitch=angle,
            n=entry["n"],
            mean_fit=round(entry["weighted"] / entry["n"], 3) if entry["n"] else 0.0,
            qualified=entry["qualified"],
            distribution=[entry["distribution"].get(level, 0) for level in FIT_LEVELS],
        )
        for angle, entry in pitches.items()
    ]
    # By fit, not size: the smallest lane is the strongest; "none" is not a pitch, so it goes last.
    pitch_stats.sort(key=lambda p: (p.pitch == "none", -p.mean_fit))

    calls, spend, avg_ms = raw["spend"]

    return Stats(
        cv=selected,
        cvs=catalogue,
        total=total,
        qualified=qualified,
        worth_applying=worth,
        median_fit=round(float(median or 0), 3),
        mean_fit=round(float(mean or 0), 3),
        spend_usd=float(spend or 0),
        calls=calls,
        avg_duration_ms=round(float(avg_ms or 0)),
        fit_distribution=fit_distribution,
        pitches=pitch_stats,
        countries=[NameCount(name=name, n=n) for name, n in raw["countries"]],
        work_formats=[NameCount(name=name, n=n) for name, n in raw["formats"]],
        search=await asyncio.to_thread(_search),
    )
