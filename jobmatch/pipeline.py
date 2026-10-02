"""discover -> persist -> fetch -> persist -> match -> persist. The one module
allowed to know about sources, the matcher and the database at once.

Failure isolation is structural: each stage of each vacancy is its own
transaction, and one bad page or one failed API call costs a warning, not the
run. Nothing here remembers where it got to, because every guard is
state-based — the next run resumes by looking at the rows, not at a cursor.

Async throughout, on one event loop: the overlap that matters — pages in
flight, Jev calls in flight — is waiting, and coroutines wait for free.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import aclosing
from dataclasses import dataclass, field
from decimal import Decimal

from . import matching, repository
from .config import Settings, SourceConfig
from .db import SessionLocal
from .models import MAX_FETCH_ATTEMPTS, LlmCall, Vacancy
from .sources import SOURCES, Listing, Rate, Source, VacancyGone, source_for_url

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunReport:
    """Counters for one run.

    Bumped directly from concurrent workers, with no lock: they are coroutines
    on one thread, and `self.fetched += 1` holds no `await`, so nothing can
    interleave inside it.
    """

    discovered: int = 0
    added: int = 0            # rows the feed had not shown us before
    fetched: int = 0
    skipped: int = 0          # already fetched, delisted, or out of attempts
    delisted: int = 0
    matched: int = 0          # calls actually paid for
    already_matched: int = 0  # the free case; the point of the fingerprint
    cost: Decimal = Decimal(0)
    failures: list[tuple[str, Exception]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"discovered {self.discovered} ({self.added} new), "
            f"fetched {self.fetched}, "
            f"skipped {self.skipped}, delisted {self.delisted}, "
            f"matched {self.matched} (${self.cost:.6f}), "
            f"already matched {self.already_matched}, failed {len(self.failures)}"
        )


async def run(
    settings: Settings, limit: int | None = None, *, match: bool = True
) -> RunReport:
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
    for entry, sweep, countries in settings.crawls():
        source = SOURCES[entry.name]     # validated at load
        log.info("sweep %s: %s", sweep, ", ".join(countries))
        listings = await _safe_discover(source, entry, countries, settings, report)
        report.discovered += len(listings)
        await _process_all(source, listings, settings, report)

    if match:
        await match_all(settings, report=report, limit=limit)
    return report


async def discover_all(settings: Settings, *, limit: int | None = None) -> RunReport:
    """Phase 1 alone: store what the sweeps find, fetch nothing.

    The honest completion of the state-based design -- discovery already leaves
    rows that are a valid state (`fetched_at IS NULL` *is* the fetch queue), so
    stopping here loses nothing and `jobmatch fetch --pending` resumes.

    What it is for: seeing what a changed search or a new country actually
    yields, without then pulling thousands of pages. ``limit`` stops the walk
    early, so that costs a page rather than a sweep.
    """
    report = RunReport()
    for entry, sweep, countries in settings.crawls():
        if limit is not None and report.discovered >= limit:
            break
        source = SOURCES[entry.name]
        log.info("sweep %s: %s", sweep, ", ".join(countries))
        remaining = None if limit is None else limit - report.discovered
        listings = await _safe_discover(source, entry, countries, settings, report, remaining)
        report.discovered += len(listings)
        for listing in listings:
            await _persist_listing(source, listing, report)
    return report


async def match_all(
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

    `matching.CONCURRENCY` calls are in flight at once. ``limit`` is still
    exact under that: a slot is taken *before* a call goes out, not counted
    after it lands, so N workers cannot all see "one left" and spend N.
    """
    report = report if report is not None else RunReport()
    async with SessionLocal() as session:
        candidates = await repository.matchable_vacancy_ids(
            session, [entry.name for entry in settings.sources], country
        )
    if not candidates:
        return report

    cv = matching.load_cv(settings.cv_path)   # read and sanitized once per run
    budget = _Budget(limit)
    async with matching.open_client() as client:    # one HTTP session per run
        async def match_one(vacancy_id: int) -> None:
            await _match(vacancy_id, cv, client, settings, report, budget)

        await _drain(
            candidates, match_one, matching.CONCURRENCY,
            failed=lambda vacancy_id, exc: report.failures.append((f"match:{vacancy_id}", exc)),
            stop=budget.spent,
        )
    if budget.spent():
        log.info("stopped at --limit %d matches", limit)
    return report


class _Budget:
    """How many paid calls are left. ``take`` and ``give_back`` hold no
    ``await``, so a check and its reservation cannot be split by another
    worker."""

    def __init__(self, limit: int | None) -> None:
        self.left = limit

    def spent(self) -> bool:
        return self.left is not None and self.left <= 0

    def take(self) -> bool:
        if self.left is None:
            return True
        if self.left <= 0:
            return False
        self.left -= 1
        return True

    def give_back(self) -> None:
        """A failed call does not count against ``--limit``, as before."""
        if self.left is not None:
            self.left += 1


async def _drain[T](
    work: Iterable[T],
    handle: Callable[[T], Awaitable[None]],
    workers: int,
    *,
    failed: Callable[[T, Exception], None],
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Run ``handle`` over ``work`` with at most ``workers`` in flight.

    Workers share one iterator, so each item is taken exactly once; ``next()``
    holds no ``await``, which is what makes sharing it safe.

    An error ``handle`` did not catch costs that item, never its neighbours.
    Left to reach the TaskGroup it would cancel every other worker, and a
    cancelled Jev call is billed yet never reaches `llm_calls` --
    ``CancelledError`` is not an ``Exception``, so the ledger write is skipped.
    Two concurrent runs make this real: the loser's `save_match` raises.
    """
    items = iter(work)

    async def worker() -> None:
        for item in items:
            if stop():
                return
            try:
                await handle(item)
            except Exception as exc:
                log.warning("%r failed: %s", item, exc)
                failed(item, exc)

    async with asyncio.TaskGroup() as group:
        for _ in range(max(1, workers)):
            group.create_task(worker())


async def fetch_all(
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
    async with SessionLocal() as session:
        pending = await repository.fetch_queue(
            session, [entry.name for entry in settings.sources], limit
        )
    if not pending:
        log.info("fetch queue is empty")
        return report

    source = source_for_url(pending[0][1])
    rate = settings.rate_for(source.rate)
    log.info("fetching %d pending pages, %d at a time", len(pending), rate.workers)
    work = [
        (vacancy_id, Listing(external_id=str(vacancy_id), url=url))
        for vacancy_id, url in pending
    ]
    await _fetch_concurrently(source, work, settings, report)
    return report


async def _safe_discover(
    source: Source,
    entry: SourceConfig,
    countries: tuple[str, ...],
    settings: Settings,
    report: RunReport,
    limit: int | None = None,
) -> list[Listing]:
    """A source being down costs one warning, not the other sources' work.

    Consumed item by item rather than with ``list()`` so that a crawl which
    dies on page four keeps the three pages it already walked. The failure is
    still recorded — a partial crawl must not pass for a complete one.

    ``limit`` stops the walk rather than trimming the result, so trying a config
    change costs a page or two instead of the whole sweep.
    """
    listings: list[Listing] = []
    try:
        # aclosing: breaking out of an async generator does not close it, and
        # an unclosed walk would hold its HTTP client open until GC
        async with aclosing(
            source.discover(entry.search, settings.crawl, countries)
        ) as walk:
            async for listing in walk:
                listings.append(listing)
                if limit is not None and len(listings) >= limit:
                    log.info("stopping at --limit %d listings", limit)
                    break
    except Exception as exc:
        log.warning("discovery failed for %s after %d: %s", source.name, len(listings), exc)
        report.failures.append((f"discover:{source.name}:{','.join(countries)}", exc))
    return listings


async def _process_all(
    source: Source, listings: list[Listing], settings: Settings, report: RunReport
) -> None:
    """Upsert every listing, then fetch the outstanding ones concurrently.

    Fetching is almost entirely waiting on hh, so it is the one phase worth
    overlapping: `rate.workers` pages are in flight at once, each worker still
    sleeping `rate.delay` after its own page, so the rate is `workers / delay`
    per second. The source states its own measured value (`sources/hh` RATE).
    """
    pending: list[tuple[int, Listing]] = []
    for listing in listings:                       # upserts stay sequential:
        vacancy_id = await _persist_listing(source, listing, report)   # one row
        if vacancy_id is not None:                 # each, and they are cheap
            pending.append((vacancy_id, listing))

    await _fetch_concurrently(source, pending, settings, report)


async def _fetch_concurrently(
    source: Source,
    work: list[tuple[int, Listing]],
    settings: Settings,
    report: RunReport,
) -> None:
    rate = settings.rate_for(source.rate)

    async def fetch_one(item: tuple[int, Listing]) -> None:
        vacancy_id, listing = item
        await _fetch(source, listing, vacancy_id, rate, report)

    await _drain(
        work, fetch_one, rate.workers,
        failed=lambda item, exc: report.failures.append((item[1].url, exc)),
    )


async def _persist_listing(source: Source, listing: Listing, report: RunReport) -> int | None:
    """Committed before any network call, so a failure downstream still has a
    row to be recorded against. Returns None when there is nothing more to do."""
    async with SessionLocal.begin() as session:
        vacancy = await repository.upsert_listing(session, source.name, listing)
        # one statement sets both from the same transaction clock on insert and
        # only last_seen_at on conflict, so equal means this row is new
        if vacancy.first_seen_at == vacancy.last_seen_at:
            report.added += 1
        if vacancy.delisted_at is not None:
            report.skipped += 1
            return None
        return vacancy.id


async def _fetch(
    source: Source, listing: Listing, vacancy_id: int, rate: Rate, report: RunReport
) -> None:
    # No session is held across the page fetch: under AsyncSession an open
    # session pins a pooled connection, and the pool would have to be as wide
    # as the number of pages in flight.
    async with SessionLocal() as session:
        vacancy = await session.get_one(Vacancy, vacancy_id)
        if (
            vacancy.fetched_at is not None
            or vacancy.fetch_attempts >= MAX_FETCH_ATTEMPTS
        ):
            report.skipped += 1
            return

    try:
        data = await source.fetch(listing.url)
    except VacancyGone:
        async with SessionLocal.begin() as session:
            await repository.mark_delisted(session, vacancy_id)
        report.delisted += 1
        log.info("delisted %s", listing.url)
    except Exception as exc:
        async with SessionLocal.begin() as session:
            await repository.record_fetch_failure(session, vacancy_id, exc)
        report.failures.append((listing.url, exc))
        log.warning("fetch failed for %s: %s", listing.url, exc)
    else:
        async with SessionLocal.begin() as session:
            await repository.apply_fetched(session, vacancy_id, data)
        report.fetched += 1
        log.info("fetched %s — %s", listing.url, data.title)
    finally:
        # per worker: with N workers the rate is N/delay per second. Pages are
        # ~700KB. The number is the source's own -- see sources/hh RATE.
        await asyncio.sleep(rate.delay)


async def _match(
    vacancy_id: int, cv: str, client, settings: Settings, report: RunReport, budget: _Budget
) -> None:
    # one transaction: make_current may revive a superseded row, which is a
    # write, and the commonest case (already current) writes nothing at all
    async with SessionLocal.begin() as session:
        vacancy = await session.get_one(Vacancy, vacancy_id)
        if not vacancy.description:
            return
        job = matching.job_text(vacancy)
        fingerprint = matching.inputs_fingerprint(cv, job, matching.QUESTIONS, settings.model)
        if await repository.make_current(session, vacancy_id, fingerprint):
            report.already_matched += 1   # free, whether found current or revived
            return

    if not budget.take():
        return
    try:
        outcome = await matching.match_vacancy(
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
        budget.give_back()
        report.failures.append((vacancy.url, exc))
        log.warning("match failed for %s: %s", vacancy.url, exc)
        return

    async with SessionLocal.begin() as session:
        await repository.save_match(session, vacancy_id, outcome)
        cost = (await session.get_one(LlmCall, outcome.call_id)).cost_usd
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
