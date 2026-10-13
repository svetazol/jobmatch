"""The ranked list and one vacancy. Read-only, apart from drafting a cover note."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ... import cover_note, repository
from ...config import DEFAULT_PATH, load_settings
from ...models import MatchResult, Vacancy
from ..deps import get_session
from ..schemas import CoverNote, MatchSummary, VacancyDetail, VacancyPage, VacancyRow

router = APIRouter(prefix="/api/vacancies", tags=["vacancies"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


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
    """Ranked by fit (or newest first), filtered, keyset-paginated.

    A cursor is only valid for the `sort` it was issued under.

    `pitch` is the UI's word for `best_angle`; the column keeps its name and
    this is the only place the two meet.
    """
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
        # "null" when the page ended on an unmatched (or undated) vacancy —
        # those sort last and still have to be pageable, so the cursor has to
        # be able to say "I am inside the NULL tail, at this id".
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
        # rsplit: an ISO timestamp has colons of its own
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
        # the complete record, so the client reads level descriptions from the
        # stored legend rather than hardcoding questions.py
        answers=match.answers if match else None,
    )


@router.post("/{vacancy_id}/cover-note", response_model=CoverNote)
async def write_cover_note(vacancy_id: int, session: SessionDep) -> CoverNote:
    """Why the CV at `cv_path` suits this vacancy, drafted by `claude -p`.

    Takes 10-30 s. A 502 means `claude` itself failed (not installed, not
    signed in, timed out); its message is passed through so the UI can show it.
    """
    found = await repository.get_vacancy(session, vacancy_id)
    if found is None:
        raise HTTPException(404, f"no vacancy {vacancy_id}")
    vacancy, match = found
    cv = load_settings(DEFAULT_PATH).cv_path.read_text(encoding="utf-8")
    try:
        text = await cover_note.write(vacancy, match, cv)
    except cover_note.CoverNoteError as e:
        raise HTTPException(502, str(e)) from None
    return CoverNote(text=text, language=cover_note.language_of(vacancy))
