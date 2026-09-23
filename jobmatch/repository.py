"""The only module that writes SQL. A flat set of functions, not a layer."""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import Vacancy
from .sources import VacancyData


def upsert_vacancy(session: Session, source: str, data: VacancyData) -> Vacancy:
    """Store a fetched vacancy. Re-running updates the row, never duplicates it.

    One statement, so there is no read-modify-write race. ``first_seen_at``
    and the triage columns belong to the row, not to this fetch, and are left
    alone on conflict.
    """
    values = {
        "source": source,
        "external_id": data.external_id,
        "url": data.url,
        "title": data.title,
        "company": data.company,
        "salary_raw": data.salary,
        "experience_raw": data.experience,
        "description": data.description,
        "skills": data.skills,
        "published_at": data.published_at,
        "raw": data.raw,
        "last_seen_at": func.now(),
        "fetched_at": func.now(),
        "fetch_attempts": 0,
        "fetch_error": None,
        "delisted_at": None,
    }
    stmt = insert(Vacancy).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_vacancies_source_external_id",
        set_={
            **{k: stmt.excluded[k] for k in values if k not in ("source", "external_id")},
            # merge rather than replace: another phase may have put keys here
            "raw": Vacancy.raw + stmt.excluded.raw,
            "updated_at": func.now(),
        },
    ).returning(Vacancy)
    return session.scalars(stmt).one()
