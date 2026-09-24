"""The only module that writes SQL. A flat set of functions, not a layer.

Persistence is two-phase, because discovery and fetching know different
things. Discovery owns ``last_seen_at`` and never touches ``description`` or
``fetched_at``; fetching fills the rest. A half-filled row is a valid state —
it *is* the fetch queue.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .matching import MatchOutcome
from .models import MatchResult, Vacancy
from .sources import Listing, VacancyData


def upsert_listing(session: Session, source: str, listing: Listing) -> Vacancy:
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
    return session.scalars(stmt).one()


def apply_fetched(session: Session, vacancy_id: int, data: VacancyData) -> Vacancy:
    """Fill in everything the detail page knows. Clears any earlier failure."""
    vacancy = session.get_one(Vacancy, vacancy_id)
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


def store_fetched(session: Session, source: str, data: VacancyData) -> Vacancy:
    """Both phases at once, for `jobmatch fetch <url>` — no feed involved."""
    vacancy = upsert_listing(
        session,
        source,
        Listing(
            external_id=data.external_id,
            url=data.url,
            title=data.title,
            published_at=data.published_at,
        ),
    )
    session.flush()
    return apply_fetched(session, vacancy.id, data)


def record_fetch_failure(session: Session, vacancy_id: int, error: Exception) -> None:
    """Count the failure so a permanently broken page stops being retried."""
    vacancy = session.get_one(Vacancy, vacancy_id)
    vacancy.fetch_attempts += 1
    vacancy.fetch_error = f"{type(error).__name__}: {error}"[:2000]


def mark_delisted(session: Session, vacancy_id: int) -> None:
    """Only ever called on a positive signal — a 404, not an absence."""
    vacancy = session.get_one(Vacancy, vacancy_id)
    vacancy.delisted_at = func.now()
    vacancy.fetch_error = None


def matchable_vacancy_ids(
    session: Session, sources: Sequence[str], country: str | None = None
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
        session.scalars(
            query
            .order_by(Vacancy.published_at.desc().nullslast(), Vacancy.id)
        )
    )


def make_current(session: Session, vacancy_id: int, fingerprint: str) -> bool:
    """Is this exact request already answered? If so, make that answer current.

    The whole of "never pay twice", and a little more: an answer is looked up
    by fingerprint regardless of whether it is the current one, so reverting a
    CV to an earlier version *revives* the matching row instead of buying it
    again. Without the revival the old answer would be found and skipped while
    a row computed from a different CV stayed flagged as current.
    """
    existing = session.scalar(
        select(MatchResult).where(
            MatchResult.vacancy_id == vacancy_id,
            MatchResult.inputs_fingerprint == fingerprint,
        )
    )
    if existing is None:
        return False
    if existing.superseded_at is None:
        return True                      # already current: no writes at all

    _supersede_current(session, vacancy_id)
    existing.superseded_at = None
    return True


def _supersede_current(session: Session, vacancy_id: int) -> None:
    session.execute(
        update(MatchResult)
        .where(MatchResult.vacancy_id == vacancy_id, MatchResult.superseded_at.is_(None))
        .values(superseded_at=func.now())
    )
    session.flush()


def save_match(session: Session, vacancy_id: int, outcome: MatchOutcome) -> MatchResult:
    """Supersede whatever was current, then insert. Append-only otherwise.

    Only ever called after a paid call — an answer that already exists is
    handled by ``make_current``. A partial unique index makes "one current row
    per vacancy" the database's problem, so two concurrent runs cannot both
    leave a current row; the loser gets an IntegrityError.
    """
    _supersede_current(session, vacancy_id)
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
        answers=outcome.answers,
    )
    session.add(result)
    # the cheap pre-check for "the employer edited the posting"
    session.get_one(Vacancy, vacancy_id).content_hash = outcome.vacancy_content_hash
    return result


def count_vacancies(session: Session) -> tuple[int, int, int]:
    """(stored, fetched, matched) — what `run` prints at the end."""
    stored = session.scalar(select(func.count()).select_from(Vacancy)) or 0
    fetched = (
        session.scalar(
            select(func.count()).select_from(Vacancy).where(Vacancy.fetched_at.is_not(None))
        )
        or 0
    )
    matched = (
        session.scalar(
            select(func.count())
            .select_from(MatchResult)
            .where(MatchResult.superseded_at.is_(None))
        )
        or 0
    )
    return stored, fetched, matched
