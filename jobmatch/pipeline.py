"""discover -> persist -> fetch -> persist -> match -> persist. The one module
allowed to know about sources, the matcher and the database at once.

Failure isolation is structural: each stage of each vacancy is its own
transaction, and one bad page or one failed API call costs a warning, not the
run. Nothing here remembers where it got to, because every guard is
state-based — the next run resumes by looking at the rows, not at a cursor.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from . import matching, repository
from .config import Settings, SourceConfig
from .db import SessionLocal
from .models import LlmCall, Vacancy
from .sources import SOURCES, Listing, Source, VacancyGone

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunReport:
    discovered: int = 0
    fetched: int = 0
    skipped: int = 0          # already fetched, delisted, or out of attempts
    delisted: int = 0
    matched: int = 0          # calls actually paid for
    already_matched: int = 0  # the free case; the point of the fingerprint
    cost: Decimal = Decimal(0)
    failures: list[tuple[str, Exception]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"discovered {self.discovered}, fetched {self.fetched}, "
            f"skipped {self.skipped}, delisted {self.delisted}, "
            f"matched {self.matched} (${self.cost:.6f}), "
            f"already matched {self.already_matched}, failed {len(self.failures)}"
        )


def run(settings: Settings, limit: int | None = None) -> RunReport:
    """Two phases, deliberately separate.

    Discovery and fetching walk today's feed. Matching walks the *table*,
    because the feed is a rolling window and the corpus outgrows it — a
    vacancy stored last week still needs an answer when the CV changes.

    ``limit`` caps the matching phase, which is the phase that costs money:
    the sane way to try a change against 2–3 vacancies before twenty.
    """
    report = RunReport()
    for entry in settings.sources:
        source = SOURCES.get(entry.name)
        if source is None:
            log.error("config names unknown source %r; skipping", entry.name)
            continue
        for listing in _safe_discover(source, entry, settings, report):
            report.discovered += 1
            _process_one(source, listing, settings, report)

    match_all(settings, report=report, limit=limit)
    return report


def match_all(
    settings: Settings,
    *,
    report: RunReport | None = None,
    limit: int | None = None,
    country: str | None = None,
) -> RunReport:
    """The paid phase on its own, over what is already stored.

    `run()` calls this after crawling; `jobmatch match` calls it without
    crawling at all, which is how you re-match after editing the CV or the
    questions without touching the site.
    """
    report = report if report is not None else RunReport()
    with SessionLocal() as session:
        candidates = repository.matchable_vacancy_ids(
            session, [entry.name for entry in settings.sources], country
        )
    if not candidates:
        return report

    cv = matching.load_cv(settings.cv_path)   # read and sanitized once per run
    with matching.open_client() as client:    # one HTTP session per run
        for vacancy_id in candidates:
            if limit is not None and report.matched >= limit:
                log.info("stopping at --limit %d matches", limit)
                break
            _match(vacancy_id, cv, client, settings, report)
    return report


def _safe_discover(
    source: Source, entry: SourceConfig, settings: Settings, report: RunReport
) -> list[Listing]:
    """A source being down costs one warning, not the other sources' work.

    Consumed item by item rather than with ``list()`` so that a crawl which
    dies on page four keeps the three pages it already walked. The failure is
    still recorded — a partial crawl must not pass for a complete one.
    """
    listings: list[Listing] = []
    try:
        for listing in source.discover(entry.params, settings.crawl):
            listings.append(listing)
    except Exception as exc:
        log.warning("discovery failed for %s after %d: %s", source.name, len(listings), exc)
        report.failures.append((f"discover:{source.name}", exc))
    return listings


def _process_one(
    source: Source, listing: Listing, settings: Settings, report: RunReport
) -> None:
    vacancy_id = _persist_listing(source, listing, report)
    if vacancy_id is not None:
        _fetch(source, listing, vacancy_id, settings, report)


def _persist_listing(source: Source, listing: Listing, report: RunReport) -> int | None:
    """Committed before any network call, so a failure downstream still has a
    row to be recorded against. Returns None when there is nothing more to do."""
    with SessionLocal.begin() as session:
        vacancy = repository.upsert_listing(session, source.name, listing)
        if vacancy.delisted_at is not None:
            report.skipped += 1
            return None
        return vacancy.id


def _fetch(
    source: Source, listing: Listing, vacancy_id: int, settings: Settings, report: RunReport
) -> None:
    with SessionLocal() as session:
        vacancy = session.get_one(Vacancy, vacancy_id)
        if (
            vacancy.fetched_at is not None
            or vacancy.fetch_attempts >= settings.max_fetch_attempts
        ):
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
        # only after a page hit; pages are ~700KB, which is the real argument
        # for the delay
        time.sleep(settings.fetch_delay)


def _match(vacancy_id: int, cv: str, client, settings: Settings, report: RunReport) -> None:
    # one transaction: make_current may revive a superseded row, which is a
    # write, and the commonest case (already current) writes nothing at all
    with SessionLocal.begin() as session:
        vacancy = session.get_one(Vacancy, vacancy_id)
        if not vacancy.description:
            return
        job = matching.job_text(vacancy)
        fingerprint = matching.inputs_fingerprint(cv, job, matching.QUESTIONS, settings.model)
        if repository.make_current(session, vacancy_id, fingerprint):
            report.already_matched += 1   # free, whether found current or revived
            return

    try:
        outcome = matching.match_vacancy(
            client,
            cv,
            vacancy,
            settings.model,
            fingerprint=fingerprint,
            cv_hash=matching.sha256(cv),
            content_hash=matching.content_hash(job),
            job=job,
        )
    except Exception as exc:
        # the llm_calls row is already committed, in its own session, so the
        # spend and the reason are on record even though this raised
        report.failures.append((vacancy.url, exc))
        log.warning("match failed for %s: %s", vacancy.url, exc)
        return

    with SessionLocal.begin() as session:
        repository.save_match(session, vacancy_id, outcome)
        cost = session.get_one(LlmCall, outcome.call_id).cost_usd
    report.matched += 1
    report.cost += cost or Decimal(0)
    log.info(
        "matched %s — %s %.2f, angle %s, gap %s",
        vacancy.url,
        outcome.overall_fit_label,
        outcome.overall_fit_score,
        outcome.best_angle,
        outcome.top_gap,
    )
