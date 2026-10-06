"""The only module that writes SQL. A flat set of functions, not a layer.

Persistence is two-phase, because discovery and fetching know different
things. Discovery owns ``last_seen_at`` and never touches ``description`` or
``fetched_at``; fetching fills the rest. A half-filled row is a valid state —
it *is* the fetch queue.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .matching import MatchOutcome
from .models import MAX_FETCH_ATTEMPTS, LlmCall, MatchResult, Vacancy
from .sources import Listing, VacancyData


async def upsert_listing(session: AsyncSession, source: str, listing: Listing) -> Vacancy:
    """Record that the feed just showed us this vacancy.

    One statement, so there is no read-modify-write race. Anything the fetch
    phase owns is left alone, and a value already stored wins over the feed's
    thinner version of it.
    """
    stmt = insert(Vacancy).values(
        source=source,
        external_id=listing.external_id,
        url=listing.url,
        title=listing.title,
        published_at=listing.published_at,
        last_seen_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_vacancies_source_external_id",
        set_={
            "last_seen_at": func.now(),
            "url": stmt.excluded.url,
            "title": func.coalesce(Vacancy.title, stmt.excluded.title),
            "published_at": func.coalesce(Vacancy.published_at, stmt.excluded.published_at),
            "updated_at": func.now(),
        },
    ).returning(Vacancy)
    return (await session.scalars(stmt)).one()


async def apply_fetched(session: AsyncSession, vacancy_id: int, data: VacancyData) -> Vacancy:
    """Fill in everything the detail page knows. Clears any earlier failure."""
    vacancy = await session.get_one(Vacancy, vacancy_id)
    vacancy.url = data.url
    vacancy.title = data.title
    vacancy.company = data.company
    vacancy.salary_raw = data.salary
    vacancy.experience_raw = data.experience
    vacancy.description = data.description
    vacancy.skills = data.skills
    vacancy.country = data.country
    vacancy.work_formats = data.work_formats
    vacancy.raw = {**vacancy.raw, **data.raw}
    if data.published_at:  # the page's date beats the feed's
        vacancy.published_at = data.published_at
    vacancy.fetched_at = func.now()
    vacancy.fetch_attempts = 0
    vacancy.fetch_error = None
    vacancy.delisted_at = None
    return vacancy


async def store_fetched(session: AsyncSession, source: str, data: VacancyData) -> Vacancy:
    """Both phases at once, for `jobmatch fetch <url>` — no feed involved."""
    vacancy = await upsert_listing(
        session,
        source,
        Listing(
            external_id=data.external_id,
            url=data.url,
            title=data.title,
            published_at=data.published_at,
        ),
    )
    await session.flush()
    return await apply_fetched(session, vacancy.id, data)


async def record_fetch_failure(session: AsyncSession, vacancy_id: int, error: Exception) -> None:
    """Count the failure so a permanently broken page stops being retried."""
    vacancy = await session.get_one(Vacancy, vacancy_id)
    vacancy.fetch_attempts += 1
    vacancy.fetch_error = f"{type(error).__name__}: {error}"[:2000]


async def mark_delisted(session: AsyncSession, vacancy_id: int) -> None:
    """Only ever called on a positive signal — a 404, not an absence."""
    vacancy = await session.get_one(Vacancy, vacancy_id)
    vacancy.delisted_at = func.now()
    vacancy.fetch_error = None


async def matchable_vacancy_ids(
    session: AsyncSession, sources: Sequence[str], country: str | None = None
) -> list[int]:
    """Every vacancy worth asking about — *not* only the ones in today's feed.

    The feed is a rolling 20-item window, so the corpus outgrows it by design.
    Matching from the table instead of from the discovery loop is what makes
    "editing the CV re-matches everything" true of the whole corpus rather
    than of the last twenty. Costs nothing when nothing changed: the
    fingerprint lookup downstream is a single indexed read per vacancy.

    Scoped to the sources config currently enables, so disabling a source
    stops the spending on it rather than quietly continuing.
    """
    query = (
        select(Vacancy.id)
        .where(
            Vacancy.source.in_(sources),
            Vacancy.fetched_at.is_not(None),
            Vacancy.description.is_not(None),
        )
    )
    if country:
        query = query.where(Vacancy.country == country)
    return list(
        await session.scalars(
            query
            .order_by(Vacancy.published_at.desc().nullslast(), Vacancy.id)
        )
    )


async def fetch_queue(
    session: AsyncSession, sources: Sequence[str], limit: int | None = None
) -> list[tuple[int, str]]:
    """Every vacancy still waiting for its detail page, as (id, url).

    The counterpart to ``matchable_vacancy_ids``: that one drives the paid
    phase over stored rows, this one drives the free phase, so neither needs
    the crawl to have just run. ``fetched_at IS NULL`` *is* the queue, which is
    why a failed fetch needs no retry table — the row simply stays in it.

    Matches the predicate on ``ix_vacancies_fetch_queue``, oldest first so a
    backlog drains in the order it arrived.
    """
    query = (
        select(Vacancy.id, Vacancy.url)
        .where(
            Vacancy.source.in_(sources),
            Vacancy.fetched_at.is_(None),
            Vacancy.delisted_at.is_(None),
            Vacancy.fetch_attempts < MAX_FETCH_ATTEMPTS,
        )
        .order_by(Vacancy.first_seen_at)
    )
    if limit is not None:
        query = query.limit(limit)
    return [(row.id, row.url) for row in await session.execute(query)]


async def make_current(session: AsyncSession, vacancy_id: int, fingerprint: str) -> bool:
    """Is this exact request already answered? If so, make that answer current.

    The whole of "never pay twice", and a little more: an answer is looked up
    by fingerprint regardless of whether it is the current one, so reverting a
    CV to an earlier version *revives* the matching row instead of buying it
    again. Without the revival the old answer would be found and skipped while
    a row computed from a different CV stayed flagged as current.
    """
    existing = await session.scalar(
        select(MatchResult).where(
            MatchResult.vacancy_id == vacancy_id,
            MatchResult.inputs_fingerprint == fingerprint,
        )
    )
    if existing is None:
        return False
    if existing.superseded_at is None:
        return True                      # already current: no writes at all

    await _supersede_current(session, vacancy_id)
    existing.superseded_at = None
    return True


async def _supersede_current(session: AsyncSession, vacancy_id: int) -> None:
    await session.execute(
        update(MatchResult)
        .where(MatchResult.vacancy_id == vacancy_id, MatchResult.superseded_at.is_(None))
        .values(superseded_at=func.now())
    )
    await session.flush()


async def save_match(session: AsyncSession, vacancy_id: int, outcome: MatchOutcome) -> MatchResult:
    """Supersede whatever was current, then insert. Append-only otherwise.

    Only ever called after a paid call — an answer that already exists is
    handled by ``make_current``. A partial unique index makes "one current row
    per vacancy" the database's problem, so two concurrent runs cannot both
    leave a current row; the loser gets an IntegrityError.
    """
    await _supersede_current(session, vacancy_id)
    result = MatchResult(
        vacancy_id=vacancy_id,
        llm_call_id=outcome.call_id,
        inputs_fingerprint=outcome.inputs_fingerprint,
        vacancy_content_hash=outcome.vacancy_content_hash,
        cv_hash=outcome.cv_hash,
        questions_hash=outcome.questions_hash,
        is_qualified_noul=outcome.is_qualified_noul,
        overall_fit_score=outcome.overall_fit_score,
        overall_fit_label=outcome.overall_fit_label,
        overall_fit_confidence=outcome.overall_fit_confidence,
        top_gap=outcome.top_gap,
        top_gap_confidence=outcome.top_gap_confidence,
        best_angle=outcome.best_angle,
        best_angle_confidence=outcome.best_angle_confidence,
        answers=outcome.answers,
    )
    session.add(result)
    # the cheap pre-check for "the employer edited the posting"
    (await session.get_one(Vacancy, vacancy_id)).content_hash = outcome.vacancy_content_hash
    return result


async def count_vacancies(session: AsyncSession) -> tuple[int, int, int]:
    """(stored, fetched, matched) — what `run` prints at the end."""
    stored = await session.scalar(select(func.count()).select_from(Vacancy)) or 0
    fetched = (
        await session.scalar(
            select(func.count()).select_from(Vacancy).where(Vacancy.fetched_at.is_not(None))
        )
        or 0
    )
    matched = (
        await session.scalar(
            select(func.count())
            .select_from(MatchResult)
            .where(MatchResult.superseded_at.is_(None))
        )
        or 0
    )
    return stored, fetched, matched


# --------------------------------------------------------------------------
# Read paths for the API. Still the only module writing SQL — the routers do
# no querying of their own, they shape what these return.
# --------------------------------------------------------------------------

# The current match, as a correlated subquery-free join condition. Every read
# below reuses it, so "current" is defined exactly once.
_CURRENT = MatchResult.superseded_at.is_(None)

# How long a vacancy can go unseen by discovery before the UI calls it stale.
# Derived, never stored (§6): a stored flag needs a job to maintain it and is
# wrong between runs.
STALE_AFTER = dt.timedelta(days=14)


async def list_vacancies(
    session: AsyncSession,
    *,
    source: str | None = None,
    min_fit: float | None = None,
    qualified_only: bool = False,
    unseen_only: bool = False,
    unapplied_only: bool = False,
    posted_days: int | None = None,
    best_angle: Sequence[str] | None = None,
    country: Sequence[str] | None = None,
    work_format: Sequence[str] | None = None,
    q: str | None = None,
    cursor: tuple[float | None, int] | None = None,
    limit: int = 50,
) -> tuple[list[tuple[Vacancy, MatchResult | None]], int]:
    """The ranked list, plus the total the same filters would return.

    A LEFT JOIN, deliberately: a fetched-but-unmatched vacancy is a real state
    the UI shows as "not matched yet", not a row to drop. That is also why the
    sort takes NULLS LAST — unmatched rows sit at the bottom rather than
    pretending to score zero.
    """
    stmt = (
        select(Vacancy, MatchResult)
        .outerjoin(MatchResult, and_(MatchResult.vacancy_id == Vacancy.id, _CURRENT))
        .where(Vacancy.hidden_at.is_(None))
    )

    if source:
        stmt = stmt.where(Vacancy.source == source)
    if min_fit is not None:
        # A threshold on the score, never on the label: the labels overlap
        # (weak spans .13-.56, good .43-.62) because the label is the most
        # probable level while the score is the expectation.
        stmt = stmt.where(MatchResult.overall_fit_score >= min_fit)
    if qualified_only:
        stmt = stmt.where(MatchResult.is_qualified.is_(True))
    if unseen_only:
        stmt = stmt.where(Vacancy.seen_at.is_(None))
    if unapplied_only:
        stmt = stmt.where(Vacancy.applied_at.is_(None))
    if posted_days:
        # the DB clock, like every other timestamp here (§6); an undated
        # posting cannot be shown to be recent, so it drops out
        stmt = stmt.where(
            Vacancy.published_at >= func.now() - dt.timedelta(days=posted_days)
        )
    if best_angle:
        stmt = stmt.where(MatchResult.best_angle.in_(list(best_angle)))
    if country:
        stmt = stmt.where(Vacancy.country.in_(list(country)))
    if work_format:
        stmt = stmt.where(Vacancy.work_formats.overlap(list(work_format)))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Vacancy.title.ilike(pattern),
                Vacancy.company.ilike(pattern),
                # a skill match, without a GIN index: array_to_string keeps it
                # one expression and the corpus is small enough to scan
                func.array_to_string(Vacancy.skills, " ").ilike(pattern),
            )
        )

    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0

    if cursor is not None:
        # Keyset on the same (score DESC NULLS LAST, id) the ORDER BY uses.
        #
        # The NULL arm is not a nicety: an unmatched vacancy has no score, so
        # `score < last_score` is NULL for it, and a naive predicate drops
        # every unmatched row from page two onwards -- silently, since they
        # simply never appear.
        last_score, last_id = cursor
        score = MatchResult.overall_fit_score
        if last_score is None:
            # already inside the NULL block: only later ids remain
            stmt = stmt.where(and_(score.is_(None), Vacancy.id > last_id))
        else:
            stmt = stmt.where(
                or_(
                    score < last_score,
                    and_(score == last_score, Vacancy.id > last_id),
                    score.is_(None),          # the NULLS LAST tail
                )
            )

    stmt = stmt.order_by(
        MatchResult.overall_fit_score.desc().nullslast(), Vacancy.id
    ).limit(limit)

    return list((await session.execute(stmt)).all()), total


async def get_vacancy(session: AsyncSession, vacancy_id: int) -> tuple[Vacancy, MatchResult | None] | None:
    """One vacancy and its current match, or None when the id is unknown."""
    row = (await session.execute(
        select(Vacancy, MatchResult)
        .outerjoin(MatchResult, and_(MatchResult.vacancy_id == Vacancy.id, _CURRENT))
        .where(Vacancy.id == vacancy_id)
    )).first()
    return (row[0], row[1]) if row else None


async def call_cost(session: AsyncSession, match: MatchResult | None) -> Decimal | None:
    """What the current match cost. Lives on llm_calls, not match_results."""
    if match is None:
        return None
    return await session.scalar(select(LlmCall.cost_usd).where(LlmCall.id == match.llm_call_id))


async def set_triage(session: AsyncSession, vacancy_id: int, field: str, on: bool) -> bool:
    """Set or clear one triage timestamp. False when the id is unknown.

    Clearing is a first-class operation, not an afterthought: it is what the
    UI's Undo sends after a hide.
    """
    column = {"seen": Vacancy.seen_at, "starred": Vacancy.starred_at,
              "hidden": Vacancy.hidden_at, "applied": Vacancy.applied_at}[field]
    result = await session.execute(
        update(Vacancy)
        .where(Vacancy.id == vacancy_id)
        .values({column: func.now() if on else None})
    )
    return result.rowcount > 0


def _cv_scope(cv_hash: str):
    """The one row this CV holds for each vacancy: its latest.

    A CV can answer the same vacancy more than once — a reworded question set
    is a new fingerprint under the same CV — and the market view wants one
    answer per posting either way, so the newest wins. `id` breaks a tie on
    `created_at`, which a bulk insert can hand out identically.
    """
    return MatchResult.id.in_(
        select(MatchResult.id)
        .where(MatchResult.cv_hash == cv_hash)
        .distinct(MatchResult.vacancy_id)
        .order_by(
            MatchResult.vacancy_id,
            MatchResult.created_at.desc(),
            MatchResult.id.desc(),
        )
    )


async def stats(
    session: AsyncSession,
    *,
    country: Sequence[str] | None = None,
    cv_hash: str | None = None,
) -> dict[str, Any]:
    """Everything the market view shows, in four queries rather than eight.

    Scoped to current matches only; a superseded row answered a different
    question set and must not be counted beside the answers that replaced it.

    ``cv_hash`` replaces that scope rather than narrowing it: one CV's answers
    to the whole corpus, current or superseded. Superseded does not mean
    wrong — it means another CV answered afterwards — and only one row per
    vacancy can be current, so without this a finished re-match would leave
    the older CV with nothing to show and a running one would split the corpus
    between the two, averaging a median over answers from neither.

    ``country`` narrows every aggregate to postings in those countries — the
    one exception is the country breakdown itself, which stays over the whole
    corpus so the filter's own options do not disappear as soon as one is
    picked.
    """
    where = [_cv_scope(cv_hash) if cv_hash else _CURRENT]
    if country:
        where.append(Vacancy.country.in_(list(country)))

    # The join is unconditional: MatchResult.vacancy_id is a non-null FK, so it
    # adds no rows, and one shape is easier to trust than two.
    def matched(*columns):
        return (
            select(*columns)
            .join(Vacancy, Vacancy.id == MatchResult.vacancy_id)
            .where(and_(*where))
        )

    headline = (await session.execute(
        matched(
            func.count(),
            func.count().filter(MatchResult.is_qualified),
            func.count().filter(
                and_(MatchResult.is_qualified, MatchResult.overall_fit_score >= 0.5)
            ),
            func.percentile_cont(0.5).within_group(MatchResult.overall_fit_score),
            func.avg(MatchResult.overall_fit_score),
        )
    )).one()

    fit_rows = (await session.execute(
        matched(MatchResult.overall_fit_label, func.count())
        .group_by(MatchResult.overall_fit_label)
    )).all()

    # One pass for both the per-pitch totals and their fit breakdown: the
    # counts have to agree, and two queries are two chances to disagree.
    pitch_rows = (await session.execute(
        matched(
            MatchResult.best_angle,
            MatchResult.overall_fit_label,
            func.count(),
            func.avg(MatchResult.overall_fit_score),
            func.count().filter(MatchResult.is_qualified),
        )
        .group_by(MatchResult.best_angle, MatchResult.overall_fit_label)
    )).all()

    spend_stmt = select(
        func.count(), func.coalesce(func.sum(LlmCall.cost_usd), 0), func.avg(LlmCall.duration_ms)
    )
    if cv_hash:
        # llm_calls has no cv_hash of its own — it records the request, not
        # what the request was about — so the spend for one CV is the spend on
        # the calls its matches point at. A call that errored produced no match
        # row and so cannot be attributed to any CV; it drops out here rather
        # than being guessed at.
        spend_stmt = spend_stmt.where(
            LlmCall.id.in_(select(MatchResult.llm_call_id).where(_cv_scope(cv_hash)))
        )
    if country:
        # An inner join, so calls not tied to a vacancy drop out — which is
        # what the filter asks for: spend *on these postings*.
        spend_stmt = spend_stmt.join(Vacancy, Vacancy.id == LlmCall.vacancy_id).where(
            Vacancy.country.in_(list(country))
        )
    spend = (await session.execute(spend_stmt)).one()

    # Deliberately unfiltered by country — see the docstring — but still over
    # this CV's current matches, so these counts add up to `total` instead of
    # quietly counting postings that were never matched, or matched for
    # somebody else's CV.
    country_where = [
        _cv_scope(cv_hash) if cv_hash else _CURRENT,
        Vacancy.country.is_not(None),
    ]
    countries = (await session.execute(
        select(Vacancy.country, func.count())
        .join(MatchResult, MatchResult.vacancy_id == Vacancy.id)
        .where(and_(*country_where))
        .group_by(Vacancy.country)
        .order_by(func.count().desc())
    )).all()

    formats_stmt = (
        select(func.unnest(Vacancy.work_formats).label("fmt"), func.count())
        .group_by(text("fmt"))
        .order_by(func.count().desc())
    )
    if cv_hash:
        formats_stmt = formats_stmt.join(
            MatchResult, MatchResult.vacancy_id == Vacancy.id
        ).where(_cv_scope(cv_hash))
    if country:
        formats_stmt = formats_stmt.where(Vacancy.country.in_(list(country)))
    formats = (await session.execute(formats_stmt)).all()

    return {
        "headline": headline,
        "fit_rows": fit_rows,
        "pitch_rows": pitch_rows,
        "spend": spend,
        "countries": countries,
        "formats": formats,
    }


async def cv_hashes(session: AsyncSession) -> list[tuple[str, int, dt.datetime]]:
    """Every CV the stored corpus has been matched against: (hash, vacancies
    answered, when it was last matched).

    Over every stored row, not only the current ones: a CV that has been
    re-matched away holds no current row at all, and it is still one of the
    CVs this corpus has an answer for — which is the whole point of being able
    to pick it.

    The database knows a CV only as the hash its answers were stored under —
    there is no `cvs` table and there should not be one, because the file is
    not the record: it gets edited, renamed and deleted, while the answers it
    earned stay true about the text that earned them. Putting a name back on
    a hash is the API's job, and only for the files still on disk.
    """
    return [
        (cv_hash, count, last)
        for cv_hash, count, last in (await session.execute(
            select(
                MatchResult.cv_hash,
                func.count(func.distinct(MatchResult.vacancy_id)),
                func.max(MatchResult.created_at),
            )
            .group_by(MatchResult.cv_hash)
            .order_by(func.max(MatchResult.created_at).desc())
        )).all()
    ]
