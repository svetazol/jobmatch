"""The three phases (docs/pipeline.md); each stage of each vacancy is its own transaction."""
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
    """Bumped by concurrent workers with no lock: `+= 1` holds no `await`."""

    discovered: int = 0
    added: int = 0            # rows the feed had not shown us before
    fetched: int = 0
    skipped: int = 0          # already fetched, delisted, or out of attempts
    delisted: int = 0
    matched: int = 0          # calls actually paid for
    already_matched: int = 0  # the free case
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
    """Crawl today's feed, then match the whole table; ``limit`` caps the paid phase only."""
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
    """Phase 1 alone, to see what a changed search yields; `fetch --pending` resumes."""
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
    """The paid phase over what is stored; ``limit`` is exact, as a slot is taken before the call."""
    report = report if report is not None else RunReport()
    async with SessionLocal() as session:
        candidates = await repository.matchable_vacancy_ids(
            session, [entry.name for entry in settings.sources], country
        )
    if not candidates:
        return report

    cv = matching.load_cv(settings.cv_path)
    budget = _Budget(limit)
    async with matching.open_client() as client:
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
    """Paid calls left; no ``await`` inside, so a check and its reservation cannot be split."""

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
        """A failed call does not count against ``--limit``."""
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
    """Errors are caught per item: one reaching the TaskGroup would cancel billed calls in flight."""
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
    """Drain the stored fetch queue without crawling; safe to re-run, as the rows are the queue."""
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
    """Item by item, so a crawl dying on page four keeps three; the failure is still recorded."""
    listings: list[Listing] = []
    try:
        # Breaking out does not close an async generator; its HTTP client would stay open.
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
    """Upsert every listing sequentially (cheap), then fetch the outstanding ones concurrently."""
    pending: list[tuple[int, Listing]] = []
    for listing in listings:
        vacancy_id = await _persist_listing(source, listing, report)
        if vacancy_id is not None:
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
    """Committed before any network call, so a failure has a row; None when nothing is left to do."""
    async with SessionLocal.begin() as session:
        vacancy = await repository.upsert_listing(session, source.name, listing)
        # One transaction clock sets both on insert, so equal means new.
        if vacancy.first_seen_at == vacancy.last_seen_at:
            report.added += 1
        if vacancy.delisted_at is not None:
            report.skipped += 1
            return None
        return vacancy.id


async def _fetch(
    source: Source, listing: Listing, vacancy_id: int, rate: Rate, report: RunReport
) -> None:
    # No session across the fetch: each would pin a pooled connection per page in flight.
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
        # Per worker, so N workers fetch N/delay pages a second.
        await asyncio.sleep(rate.delay)


async def _match(
    vacancy_id: int, cv: str, client, settings: Settings, report: RunReport, budget: _Budget
) -> None:
    # One transaction: make_current may revive a superseded row.
    async with SessionLocal.begin() as session:
        vacancy = await session.get_one(Vacancy, vacancy_id)
        if not vacancy.description:
            return
        job = matching.job_text(vacancy)
        fingerprint = matching.inputs_fingerprint(cv, job, matching.QUESTIONS, settings.model_for("match"))
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
            settings.model_for("match"),
            cv_hash=matching.sha256(cv),
            content_hash=matching.content_hash(job),
            job=job,
        )
    except Exception as exc:
        # The llm_calls row is already committed in its own session.
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
