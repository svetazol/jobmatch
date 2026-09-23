"""The only module that writes SQL. A flat set of functions, not a layer.

Persistence is two-phase, because discovery and fetching know different
things. Discovery owns ``last_seen_at`` and never touches ``description`` or
``fetched_at``; fetching fills the rest. A half-filled row is a valid state —
it *is* the fetch queue.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import Vacancy
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


def count_vacancies(session: Session) -> tuple[int, int]:
    """(stored, fetched) — what `run` prints at the end."""
    stored = session.scalar(select(func.count()).select_from(Vacancy)) or 0
    fetched = (
        session.scalar(
            select(func.count()).select_from(Vacancy).where(Vacancy.fetched_at.is_not(None))
        )
        or 0
    )
    return stored, fetched
