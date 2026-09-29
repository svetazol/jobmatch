"""The ranked list and one vacancy. Read-only."""
from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ... import repository
from ...models import MatchResult, Vacancy
from ..deps import get_session
from ..schemas import MatchSummary, VacancyDetail, VacancyPage, VacancyRow

router = APIRouter(prefix="/api/vacancies", tags=["vacancies"])

SessionDep = Annotated[Session, Depends(get_session)]


def _fit_probabilities(match: MatchResult | None) -> dict[str, float]:
    """The five stored probabilities, projected out of `answers`.

    Per §5 they stay in JSONB — read on every page load, never sorted or
    filtered on — but they ship with every row, because without them the list
    can only print a number, and the number alone is what the UI is trying not
    to assert.
    """
    if match is None:
        return {}
    probs = (match.answers or {}).get("overall_fit", {}).get("probabilities", {})
    return {str(k): float(v) for k, v in probs.items()}


def _is_stale(vacancy: Vacancy) -> bool:
    """Derived, never stored (§6)."""
    if vacancy.last_seen_at is None:
        return False
    return dt.datetime.now(dt.UTC) - vacancy.last_seen_at > repository.STALE_AFTER


def _summary(match: MatchResult | None) -> MatchSummary | None:
    if match is None:
        return None
    return MatchSummary(
        overall_fit_score=match.overall_fit_score,
        overall_fit_label=match.overall_fit_label,
        overall_fit_confidence=match.overall_fit_confidence,
        is_qualified=match.is_qualified,
        is_qualified_noul=match.is_qualified_noul,
        top_gap=match.top_gap,
        top_gap_confidence=match.top_gap_confidence,
        best_angle=match.best_angle,
        best_angle_confidence=match.best_angle_confidence,
        fit_probabilities=_fit_probabilities(match),
    )


def _row(vacancy: Vacancy, match: MatchResult | None) -> VacancyRow:
    """Written out field by field on purpose.

    These models are projections, not mirrors of the ORM (§12): `raw`, every
    hash and `fetch_error` are absent because no screen shows them, and an
    automatic copy would quietly start shipping whatever the next migration
    adds.
    """
    return VacancyRow(
        id=vacancy.id,
        title=vacancy.title,
        company=vacancy.company,
        country=vacancy.country,
        work_formats=list(vacancy.work_formats),
        url=vacancy.url,
        source=vacancy.source,
        published_at=vacancy.published_at,
        seen_at=vacancy.seen_at,
        starred_at=vacancy.starred_at,
        hidden_at=vacancy.hidden_at,
        delisted_at=vacancy.delisted_at,
        stale=_is_stale(vacancy),
        match=_summary(match),
    )


@router.get("", response_model=VacancyPage)
def list_vacancies(
    session: SessionDep,
    source: str | None = None,
    min_fit: Annotated[float | None, Query(ge=0, le=1)] = None,
    qualified: bool = False,
    unseen: bool = False,
    pitch: Annotated[list[str] | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    work_format: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> VacancyPage:
    """Ranked by fit, filtered, keyset-paginated.

    `pitch` is the UI's word for `best_angle`; the column keeps its name and
    this is the only place the two meet.
    """
    parsed_cursor = _parse_cursor(cursor)
    rows, total = repository.list_vacancies(
        session,
        source=source,
        min_fit=min_fit,
        qualified_only=qualified,
        unseen_only=unseen,
        best_angle=pitch,
        country=country,
        work_format=work_format,
        q=q,
        cursor=parsed_cursor,
        limit=limit,
    )
    items = [_row(vacancy, match) for vacancy, match in rows]
    next_cursor = None
    if len(rows) == limit:
        last_vacancy, last_match = rows[-1]
        # "null" when the page ended on an unmatched vacancy — those sort last
        # and still have to be pageable, so the cursor has to be able to say
        # "I am inside the NULL tail, at this id".
        score = "null" if last_match is None else last_match.overall_fit_score
        next_cursor = f"{score}:{last_vacancy.id}"
    return VacancyPage(items=items, total=total, next_cursor=next_cursor)


def _parse_cursor(cursor: str | None) -> tuple[float | None, int] | None:
    if not cursor:
        return None
    try:
        score, vacancy_id = cursor.split(":", 1)
        return (None if score == "null" else float(score)), int(vacancy_id)
    except ValueError:
        raise HTTPException(400, f"malformed cursor {cursor!r}") from None


@router.get("/{vacancy_id}", response_model=VacancyDetail)
def get_vacancy(vacancy_id: int, session: SessionDep) -> VacancyDetail:
    found = repository.get_vacancy(session, vacancy_id)
    if found is None:
        raise HTTPException(404, f"no vacancy {vacancy_id}")
    vacancy, match = found

    return VacancyDetail(
        **_row(vacancy, match).model_dump(),
        salary_raw=vacancy.salary_raw,
        experience_raw=vacancy.experience_raw,
        description=vacancy.description,
        skills=list(vacancy.skills),
        fetched_at=vacancy.fetched_at,
        # the complete record, so the client reads level descriptions from the
        # stored legend rather than hardcoding questions.py
        answers=match.answers if match else None,
        cost_usd=repository.call_cost(session, match),
    )
