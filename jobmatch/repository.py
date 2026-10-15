"""Every SQL query, as a flat module of named functions."""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal
from .models import (
    MAX_FETCH_ATTEMPTS, Employer, EmployerIdentity, LlmCall, MatchResult, RecruiterContact,
    RecruiterSearch, Vacancy,
)
from .sources import EmployerData, Listing, VacancyData

if TYPE_CHECKING:
    # A runtime import would be a cycle: matching writes its ledger rows through here.
    from .matching import MatchOutcome


async def upsert_listing(session: AsyncSession, source: str, listing: Listing) -> Vacancy:
    """One statement, so no race; a stored value wins over the feed's thinner one."""
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
    """Fill in everything the detail page knows, clearing any earlier failure."""
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
    vacancy.employer_external_id = data.employer_id
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


async def set_employer_external_id(
    session: AsyncSession, vacancy_id: int, employer_id: str
) -> None:
    """Not `apply_fetched`: rewriting the row from a new page would make the next match pay."""
    vacancy = await session.get_one(Vacancy, vacancy_id)
    vacancy.employer_external_id = employer_id


async def upsert_employer(session: AsyncSession, source: str, data: EmployerData) -> Employer:
    """A repeat lookup refreshes the row."""
    stmt = insert(Employer).values(
        source=source,
        external_id=data.external_id,
        url=data.url,
        name=data.name,
        site_url=data.site_url,
        raw=data.raw,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_employers_source_external_id",
        set_={
            "url": stmt.excluded.url,
            "name": stmt.excluded.name,
            "site_url": stmt.excluded.site_url,
            "raw": stmt.excluded.raw,
            "looked_up_at": func.now(),
            "updated_at": func.now(),
        },
    ).returning(Employer)
    return (await session.scalars(stmt)).one()


async def employer_of(session: AsyncSession, vacancy_id: int) -> Employer | None:
    """The looked-up employer behind a vacancy, or None if nobody asked yet."""
    return await session.scalar(
        select(Employer)
        .join(
            Vacancy,
            and_(
                Vacancy.source == Employer.source,
                Vacancy.employer_external_id == Employer.external_id,
            ),
        )
        .where(Vacancy.id == vacancy_id)
    )


async def identity_by_fingerprint(session: AsyncSession, fingerprint: str) -> EmployerIdentity | None:
    """The cache: the stored answer to exactly this request."""
    return await session.scalar(
        select(EmployerIdentity).where(EmployerIdentity.inputs_fingerprint == fingerprint)
    )


async def latest_identity(session: AsyncSession, vacancy_id: int) -> EmployerIdentity | None:
    """The newest answer for a vacancy, the one shown; `id` breaks a tie."""
    return await session.scalar(
        select(EmployerIdentity)
        .where(EmployerIdentity.vacancy_id == vacancy_id)
        .order_by(EmployerIdentity.created_at.desc(), EmployerIdentity.id.desc())
        .limit(1)
    )


async def save_identity(
    session: AsyncSession,
    vacancy_id: int,
    fingerprint: str,
    answer: dict[str, Any],
    homepage_ok: bool | None,
    careers_ok: bool | None,
    llm_call_id: int,
) -> EmployerIdentity:
    """Two clicks that paid at once keep the first row; the spend is in `llm_calls`."""
    await session.execute(
        insert(EmployerIdentity)
        .values(
            vacancy_id=vacancy_id,
            inputs_fingerprint=fingerprint,
            answer=answer,
            homepage_ok=homepage_ok,
            careers_ok=careers_ok,
            llm_call_id=llm_call_id,
        )
        .on_conflict_do_nothing(index_elements=[EmployerIdentity.inputs_fingerprint])
    )
    return (await session.scalars(
        select(EmployerIdentity).where(EmployerIdentity.inputs_fingerprint == fingerprint)
    )).one()


async def latest_recruiters(
    session: AsyncSession, employer_id: int, fingerprint: str
) -> RecruiterSearch | None:
    """By employer too: two employers can make one request and must not share people."""
    return await session.scalar(
        select(RecruiterSearch)
        .where(
            RecruiterSearch.employer_id == employer_id,
            RecruiterSearch.inputs_fingerprint == fingerprint,
        )
        .order_by(RecruiterSearch.created_at.desc(), RecruiterSearch.id.desc())
        .limit(1)
    )


async def save_recruiters(
    session: AsyncSession,
    employer_id: int,
    fingerprint: str,
    answer: dict[str, Any],
    llm_call_id: int,
) -> RecruiterSearch:
    """A plain insert, as the fingerprint is not unique; RETURNING hands back `created_at` too."""
    return (await session.scalars(
        insert(RecruiterSearch)
        .values(
            employer_id=employer_id,
            inputs_fingerprint=fingerprint,
            answer=answer,
            llm_call_id=llm_call_id,
        )
        .returning(RecruiterSearch)
    )).one()


async def mark_written(session: AsyncSession, linkedin_url: str) -> None:
    """Marking twice keeps the first date, as `applied_at` would if set once."""
    await session.execute(
        insert(RecruiterContact)
        .values(linkedin_url=linkedin_url)
        .on_conflict_do_nothing(index_elements=[RecruiterContact.linkedin_url])
    )


async def unmark_written(session: AsyncSession, linkedin_url: str) -> None:
    """Undo deletes the mark: a user's note, not a vacancy, so "nothing deletes" does not apply."""
    await session.execute(
        delete(RecruiterContact).where(RecruiterContact.linkedin_url == linkedin_url)
    )


async def written_dates(session: AsyncSession, urls: Sequence[str]) -> dict[str, dt.datetime]:
    """Canonical URL -> when the user wrote; only the marked ones."""
    if not urls:
        return {}
    rows = await session.execute(
        select(RecruiterContact.linkedin_url, RecruiterContact.written_at)
        .where(RecruiterContact.linkedin_url.in_(urls))
    )
    return dict(rows.tuples().all())


async def matchable_vacancy_ids(
    session: AsyncSession, sources: Sequence[str], country: str | None = None
) -> list[int]:
    """The whole stored corpus, not today's feed, so a CV edit re-matches everything."""
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
    """(id, url) still awaiting a detail page; must match ix_vacancies_fetch_queue's predicate."""
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
    """Make an existing answer current, so reverting a CV revives it rather than re-buying."""
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
    """Supersede, then insert; a partial unique index fails a concurrent loser."""
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
    (await session.get_one(Vacancy, vacancy_id)).content_hash = outcome.vacancy_content_hash
    return result


async def record_call(**values: Any) -> int:
    """Its own session, so the ledger row survives the caller's rollback."""
    async with SessionLocal.begin() as session:
        call = LlmCall(**values)
        session.add(call)
        await session.flush()
        return call.id


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


async def vacancies_by_ids(session: AsyncSession, vacancy_ids: Sequence[int]) -> list[Vacancy]:
    """The rows behind a list of ids, in no particular order; what `match --dry-run` previews."""
    return list(await session.scalars(select(Vacancy).where(Vacancy.id.in_(vacancy_ids))))


async def current_matches(session: AsyncSession, vacancy_ids: Sequence[int]) -> dict[int, MatchResult]:
    """vacancy id -> its current answer, for those that have one; the dry run's "before"."""
    return {
        m.vacancy_id: m
        for m in await session.scalars(
            select(MatchResult).where(MatchResult.vacancy_id.in_(vacancy_ids), _CURRENT)
        )
    }


async def count_current_matches(session: AsyncSession, cv_hash: str) -> int:
    """How many current answers one CV holds; what `jobmatch cv-hash` prints beside the hash."""
    return await session.scalar(
        select(func.count())
        .select_from(MatchResult)
        .where(MatchResult.cv_hash == cv_hash, _CURRENT)
    )


# --- Read paths for the API ---

_CURRENT = MatchResult.superseded_at.is_(None)

# Derived, never stored: a stored flag is wrong between runs (docs/architecture.md §6).
STALE_AFTER = dt.timedelta(days=14)

# Each DESC NULLS LAST, `id` breaking ties.
SORT_KEYS = {"fit": MatchResult.overall_fit_score, "posted": Vacancy.published_at}


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
    sort: str = "fit",
    cursor: tuple[float | dt.datetime | None, int] | None = None,
    limit: int = 50,
) -> tuple[list[tuple[Vacancy, MatchResult | None]], int]:
    """A LEFT JOIN: an unmatched vacancy is shown as "not matched yet", sorted last, not dropped."""
    stmt = (
        select(Vacancy, MatchResult)
        .outerjoin(MatchResult, and_(MatchResult.vacancy_id == Vacancy.id, _CURRENT))
        .where(Vacancy.hidden_at.is_(None))
    )

    if source:
        stmt = stmt.where(Vacancy.source == source)
    if min_fit is not None:
        # Never on the label: labels overlap (weak spans .13-.56, good .43-.62).
        stmt = stmt.where(MatchResult.overall_fit_score >= min_fit)
    if qualified_only:
        stmt = stmt.where(MatchResult.is_qualified.is_(True))
    if unseen_only:
        stmt = stmt.where(Vacancy.seen_at.is_(None))
    if unapplied_only:
        stmt = stmt.where(Vacancy.applied_at.is_(None))
    if posted_days:
        # The DB clock, like every timestamp; an undated posting drops out.
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
                # No GIN index: the corpus is small enough to scan.
                func.array_to_string(Vacancy.skills, " ").ilike(pattern),
            )
        )

    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0

    if cursor is not None:
        # Without the NULL arm, unmatched rows silently vanish from page two onwards.
        last_score, last_id = cursor
        score = SORT_KEYS[sort]
        if last_score is None:
            stmt = stmt.where(and_(score.is_(None), Vacancy.id > last_id))
        else:
            stmt = stmt.where(
                or_(
                    score < last_score,
                    and_(score == last_score, Vacancy.id > last_id),
                    score.is_(None),          # the NULLS LAST tail
                )
            )

    stmt = stmt.order_by(SORT_KEYS[sort].desc().nullslast(), Vacancy.id).limit(limit)

    return list((await session.execute(stmt)).all()), total


async def get_vacancy(session: AsyncSession, vacancy_id: int) -> tuple[Vacancy, MatchResult | None] | None:
    """One vacancy and its current match, or None when the id is unknown."""
    row = (await session.execute(
        select(Vacancy, MatchResult)
        .outerjoin(MatchResult, and_(MatchResult.vacancy_id == Vacancy.id, _CURRENT))
        .where(Vacancy.id == vacancy_id)
    )).first()
    return (row[0], row[1]) if row else None


async def set_triage(session: AsyncSession, vacancy_id: int, field: str, on: bool) -> bool:
    """Set or clear one triage timestamp (clearing is Undo); False when the id is unknown."""
    column = {"seen": Vacancy.seen_at, "starred": Vacancy.starred_at,
              "hidden": Vacancy.hidden_at, "applied": Vacancy.applied_at}[field]
    result = await session.execute(
        update(Vacancy)
        .where(Vacancy.id == vacancy_id)
        .values({column: func.now() if on else None})
    )
    return result.rowcount > 0


def _cv_scope(cv_hash: str):
    """This CV's newest row per vacancy, as a reworded question set is a second row under one CV."""
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
    """The market view, over current matches or one CV's answers current or not."""
    where = [_cv_scope(cv_hash) if cv_hash else _CURRENT]
    if country:
        where.append(Vacancy.country.in_(list(country)))

    # Always joined: a non-null FK adds no rows, and one shape is easier to trust.
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

    # One pass for totals and breakdown, so the counts cannot disagree.
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
    ).where(LlmCall.kind == "match")  # the view reports on matching, not on lookups
    if cv_hash:
        # A CV's spend is its matches' calls; an errored call has no match, so drops out.
        spend_stmt = spend_stmt.where(
            LlmCall.id.in_(select(MatchResult.llm_call_id).where(_cv_scope(cv_hash)))
        )
    if country:
        # Inner join: spend on these postings only.
        spend_stmt = spend_stmt.join(Vacancy, Vacancy.id == LlmCall.vacancy_id).where(
            Vacancy.country.in_(list(country))
        )
    spend = (await session.execute(spend_stmt)).one()

    # Not filtered by country, so the filter's options stay; still scoped so they add up to `total`.
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
    """(hash, vacancies, last matched) over every row; no `cvs` table, as CV files change."""
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


async def recent_calls(
    session: AsyncSession, *, kind: str | None = None, limit: int = 20
) -> list[LlmCall]:
    """The ledger's newest rows, for `jobmatch trace`. Read-only."""
    stmt = select(LlmCall).order_by(LlmCall.started_at.desc(), LlmCall.id.desc()).limit(limit)
    if kind:
        stmt = stmt.where(LlmCall.kind == kind)
    return list(await session.scalars(stmt))


async def get_call(session: AsyncSession, call_id: int) -> LlmCall | None:
    """One ledger row in full, request and response included."""
    return await session.get(LlmCall, call_id)
