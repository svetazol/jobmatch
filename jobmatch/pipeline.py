"""discover -> persist -> fetch -> persist -> match -> persist. The one module
allowed to know about sources, the matcher and the database at once.

Failure isolation is structural: each stage of each vacancy is its own
transaction, and one bad page or one failed API call costs a warning, not the
run. Nothing here remembers where it got to, because every guard is
state-based — the next run resumes by looking at the rows, not at a cursor.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from . import matching, repository
from .config import Settings, SourceConfig
from .db import SessionLocal
from .models import MAX_FETCH_ATTEMPTS, LlmCall, Vacancy
from .sources import SOURCES, Listing, Source, VacancyGone, source_for_url

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunReport:
    """Counters for one run.

    `record()` exists because the fetch phase is threaded: `self.fetched += 1`
    is load-add-store, not an atomic operation, so concurrent workers can lose
    an increment and the run would under-report what it actually did.
    """

    discovered: int = 0
    fetched: int = 0
    skipped: int = 0          # already fetched, delisted, or out of attempts
    delisted: int = 0
    matched: int = 0          # calls actually paid for
    already_matched: int = 0  # the free case; the point of the fingerprint
    cost: Decimal = Decimal(0)
    failures: list[tuple[str, Exception]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def record(self, counter: str) -> None:
        """Bump one counter under the lock."""
        with self._lock:
            setattr(self, counter, getattr(self, counter) + 1)

    def record_failure(self, url: str, exc: Exception) -> None:
        """`list.append` is atomic today, but that is a CPython implementation
        detail, not a promise. Take the lock."""
        with self._lock:
            self.failures.append((url, exc))

    def summary(self) -> str:
        return (
            f"discovered {self.discovered}, fetched {self.fetched}, "
            f"skipped {self.skipped}, delisted {self.delisted}, "
            f"matched {self.matched} (${self.cost:.6f}), "
            f"already matched {self.already_matched}, failed {len(self.failures)}"
        )


def run(settings: Settings, limit: int | None = None, *, match: bool = True) -> RunReport:
    """Two phases, deliberately separate.

    Discovery and fetching walk today's feed. Matching walks the *table*,
    because the feed is a rolling window and the corpus outgrows it — a
    vacancy stored last week still needs an answer when the CV changes.

    ``limit`` caps the matching phase, which is the phase that costs money:
    the sane way to try a change against 2–3 vacancies before twenty.

    ``match=False`` skips that phase entirely — crawl and fetch now, decide
    what to pay for later. Nothing is lost by splitting them: the stored rows
    are the queue, and `jobmatch match` picks up exactly where this stopped.
    """
    report = RunReport()
    for entry in settings.sources:
        source = SOURCES.get(entry.name)
        if source is None:
            log.error("config names unknown source %r; skipping", entry.name)
            continue
        listings = _safe_discover(source, entry, settings, report)
        report.discovered += len(listings)
        _process_all(source, listings, settings, report)

    if match:
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


def fetch_all(
    settings: Settings,
    *,
    report: RunReport | None = None,
    limit: int | None = None,
) -> RunReport:
    """Drain the fetch queue over what is already stored, without crawling.

    The free-phase twin of `match_all`. Useful whenever discovery already ran
    and the fetches did not: a throttled run, or a dropped connection.
    Re-running is safe because `fetched_at IS NULL` is the queue, so anything
    that succeeded simply is not in it any more -- and anything that failed
    `MAX_FETCH_ATTEMPTS` times has left it for good.
    """
    report = report if report is not None else RunReport()
    with SessionLocal() as session:
        pending = repository.fetch_queue(
            session, [entry.name for entry in settings.sources], limit
        )
    if not pending:
        log.info("fetch queue is empty")
        return report

    log.info("fetching %d pending pages, %d at a time", len(pending), settings.fetch_workers)
    work = [
        (vacancy_id, Listing(external_id=str(vacancy_id), url=url))
        for vacancy_id, url in pending
    ]
    _fetch_concurrently(source_for_url(work[0][1].url), work, settings, report)
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


def _process_all(
    source: Source, listings: list[Listing], settings: Settings, report: RunReport
) -> None:
    """Upsert every listing, then fetch the outstanding ones concurrently.

    Fetching is almost entirely waiting on hh, so it is the one phase worth
    overlapping: `fetch_workers` pages are in flight at once, each worker still
    sleeping `fetch_delay` after its own page. The request rate is therefore
    roughly `fetch_workers / fetch_delay` per second, which is the number to
    keep honest -- raising workers without raising the delay is how you get
    throttled (and hh does throttle: see sources/hh.py).

    Threads rather than asyncio: the work is `requests` + BeautifulSoup, both
    synchronous, and a thread pool buys the same overlap without an async
    rewrite of the source seam. `Source.fetch` stays a plain callable.
    """
    pending: list[tuple[int, Listing]] = []
    for listing in listings:                       # upserts stay sequential:
        vacancy_id = _persist_listing(source, listing, report)   # one row each,
        if vacancy_id is not None:                 # and they are cheap
            pending.append((vacancy_id, listing))

    _fetch_concurrently(source, pending, settings, report)


def _fetch_concurrently(
    source: Source,
    work: list[tuple[int, Listing]],
    settings: Settings,
    report: RunReport,
) -> None:
    workers = max(1, settings.fetch_workers)
    if workers == 1 or len(work) <= 1:
        for vacancy_id, listing in work:
            _fetch(source, listing, vacancy_id, settings, report)
        return

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
        futures = [
            pool.submit(_fetch, source, listing, vacancy_id, settings, report)
            for vacancy_id, listing in work
        ]
        for future in as_completed(futures):
            future.result()          # _fetch swallows its own errors; this
                                     # surfaces anything it could not


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
            or vacancy.fetch_attempts >= MAX_FETCH_ATTEMPTS
        ):
            report.record("skipped")
            return

    try:
        data = source.fetch(listing.url)
    except VacancyGone:
        with SessionLocal.begin() as session:
            repository.mark_delisted(session, vacancy_id)
        report.record("delisted")
        log.info("delisted %s", listing.url)
    except Exception as exc:
        with SessionLocal.begin() as session:
            repository.record_fetch_failure(session, vacancy_id, exc)
        report.record_failure(listing.url, exc)
        log.warning("fetch failed for %s: %s", listing.url, exc)
    else:
        with SessionLocal.begin() as session:
            repository.apply_fetched(session, vacancy_id, data)
        report.record("fetched")
        log.info("fetched %s — %s", listing.url, data.title)
    finally:
        # per worker, not global: with N workers the effective rate is
        # N/fetch_delay per second. Pages are ~700KB, which is the real
        # argument for keeping the delay at all.
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
