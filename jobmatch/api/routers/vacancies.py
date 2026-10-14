"""The ranked list and one vacancy. Read-only, apart from drafting a cover note."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ... import cover_note, matching, repository
from ...config import DEFAULT_PATH, load_settings
from ...llm import claude_cli
from ...models import MatchResult, Vacancy
from ..deps import get_session
from ..schemas import CoverNote, MatchSummary, VacancyDetail, VacancyPage, VacancyRow

router = APIRouter(prefix="/api/vacancies", tags=["vacancies"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _fit_probabilities(match: MatchResult | None) -> dict[str, float]:
    """Shipped with every row, so the list shows a distribution rather than asserting one number."""
    if match is None:
        return {}
    probs = (match.answers or {}).get("overall_fit", {}).get("probabilities", {})
    return {str(k): float(v) for k, v in probs.items()}


def _is_stale(vacancy: Vacancy) -> bool:
    """Derived, never stored; see docs/architecture.md §6."""
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
    """Field by field, so the next migration's columns are not shipped by accident."""
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
        applied_at=vacancy.applied_at,
        delisted_at=vacancy.delisted_at,
        stale=_is_stale(vacancy),
        match=_summary(match),
    )


@router.get("", response_model=VacancyPage)
async def list_vacancies(
    session: SessionDep,
    source: str | None = None,
    min_fit: Annotated[float | None, Query(ge=0, le=1)] = None,
    qualified: bool = False,
    unseen: bool = False,
    unapplied: bool = False,
    posted_days: Annotated[int | None, Query(ge=1)] = None,
    pitch: Annotated[list[str] | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    work_format: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    sort: Literal["fit", "posted"] = "fit",
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> VacancyPage:
    """Keyset-paginated; a cursor is valid only for its `sort`. `pitch` is the UI's `best_angle`."""
    parsed_cursor = _parse_cursor(cursor, sort)
    rows, total = await repository.list_vacancies(
        session,
        source=source,
        min_fit=min_fit,
        qualified_only=qualified,
        unseen_only=unseen,
        unapplied_only=unapplied,
        posted_days=posted_days,
        best_angle=pitch,
        country=country,
        work_format=work_format,
        q=q,
        sort=sort,
        cursor=parsed_cursor,
        limit=limit,
    )
    items = [_row(vacancy, match) for vacancy, match in rows]
    next_cursor = None
    if len(rows) == limit:
        last_vacancy, last_match = rows[-1]
        # "null": the page ended inside the NULLS LAST tail, which must stay pageable.
        if sort == "posted":
            key = last_vacancy.published_at and last_vacancy.published_at.isoformat()
        else:
            key = last_match and last_match.overall_fit_score
        next_cursor = f"{'null' if key is None else key}:{last_vacancy.id}"
    return VacancyPage(items=items, total=total, next_cursor=next_cursor)


def _parse_cursor(cursor: str | None, sort: str) -> tuple[float | dt.datetime | None, int] | None:
    if not cursor:
        return None
    try:
        # rsplit: an ISO timestamp has colons of its own.
        key, vacancy_id = cursor.rsplit(":", 1)
        if key == "null":
            return None, int(vacancy_id)
        parsed = dt.datetime.fromisoformat(key) if sort == "posted" else float(key)
        return parsed, int(vacancy_id)
    except ValueError:
        raise HTTPException(400, f"malformed cursor {cursor!r}") from None


@router.get("/{vacancy_id}", response_model=VacancyDetail)
async def get_vacancy(vacancy_id: int, session: SessionDep) -> VacancyDetail:
    found = await repository.get_vacancy(session, vacancy_id)
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
        last_seen_at=vacancy.last_seen_at,
        # Whole, so the client reads the stored legend rather than hardcoding questions.py.
        answers=match.answers if match else None,
    )


@router.post("/{vacancy_id}/cover-note", response_model=CoverNote)
async def write_cover_note(vacancy_id: int, session: SessionDep) -> CoverNote:
    """Takes 10-30 s. 502: `claude` failed, its message passed through; 422: the CV is empty."""
    found = await repository.get_vacancy(session, vacancy_id)
    if found is None:
        raise HTTPException(404, f"no vacancy {vacancy_id}")
    vacancy, match = found
    try:
        settings = load_settings(DEFAULT_PATH)
        # Sanitised, as for Jev: the CV leaves the machine here too.
        cv = matching.load_cv(settings.cv_path)
    except ValueError as e:     # an empty or unfilled CV
        raise HTTPException(422, str(e)) from None
    try:
        text = await cover_note.write(vacancy, match, cv, settings.model_for("cover_note"))
    except claude_cli.ClaudeError as e:
        raise HTTPException(502, str(e)) from None
    return CoverNote(text=text, language=cover_note.language_of(vacancy))
