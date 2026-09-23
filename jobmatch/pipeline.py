"""discover -> persist -> fetch -> persist. The one module allowed to know
about sources and the database at the same time.

Failure isolation is structural: each vacancy is its own transaction, and one
bad page costs a warning, not the run. Nothing here remembers where it got to,
because every guard is state-based — the next run resumes by looking at the
rows, not at a cursor.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import repository
from .config import Settings
from .db import SessionLocal
from .models import Vacancy
from .sources import SOURCES, Listing, Source, VacancyGone

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunReport:
    discovered: int = 0
    fetched: int = 0
    skipped: int = 0          # already fetched, delisted, or out of attempts
    delisted: int = 0
    failures: list[tuple[str, Exception]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"discovered {self.discovered}, fetched {self.fetched}, "
            f"skipped {self.skipped}, delisted {self.delisted}, "
            f"failed {len(self.failures)}"
        )


def run(settings: Settings) -> RunReport:
    report = RunReport()
    for entry in settings.sources:
        source = SOURCES.get(entry.name)
        if source is None:
            log.error("config names unknown source %r; skipping", entry.name)
            continue
        for listing in _safe_discover(source, entry, report):
            report.discovered += 1
            _process_one(source, listing, settings, report)
    return report


def _safe_discover(source: Source, entry, report: RunReport) -> list[Listing]:
    """A source being down costs one warning, not the other sources' work."""
    try:
        return list(source.discover(entry.params))
    except Exception as exc:
        log.warning("discovery failed for %s: %s", source.name, exc)
        report.failures.append((f"discover:{source.name}", exc))
        return []


def _process_one(source: Source, listing: Listing, settings: Settings, report: RunReport) -> None:
    """Two transactions: the listing is committed before the network call, so
    a fetch that blows up still leaves a row to record the failure against."""
    with SessionLocal.begin() as session:
        vacancy = repository.upsert_listing(session, source.name, listing)
        vacancy_id = vacancy.id
        needs_fetch = _needs_fetch(vacancy, settings)

    if not needs_fetch:
        report.skipped += 1
        return

    try:
        data = source.fetch(listing.url)
    except VacancyGone:
        with SessionLocal.begin() as session:
            repository.mark_delisted(session, vacancy_id)
        report.delisted += 1
        log.info("delisted %s", listing.url)
    except Exception as exc:
        with SessionLocal.begin() as session:
            repository.record_fetch_failure(session, vacancy_id, exc)
        report.failures.append((listing.url, exc))
        log.warning("fetch failed for %s: %s", listing.url, exc)
    else:
        with SessionLocal.begin() as session:
            repository.apply_fetched(session, vacancy_id, data)
        report.fetched += 1
        log.info("fetched %s — %s", listing.url, data.title)
    finally:
        # only after a page hit; a skipped vacancy costs nothing and waits for
        # nothing. Pages are ~700KB, which is the real argument for the delay.
        time.sleep(settings.fetch_delay)


def _needs_fetch(vacancy: Vacancy, settings: Settings) -> bool:
    if vacancy.fetched_at is not None:
        return False              # the whole point: a re-run re-fetches nothing
    if vacancy.delisted_at is not None:
        return False
    return vacancy.fetch_attempts < settings.max_fetch_attempts
