"""The employer behind a vacancy: read what is stored, or look it up on click."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ... import pipeline, repository
from ..deps import get_session
from ..schemas import Company

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vacancies", tags=["company"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{vacancy_id}/company", response_model=Company | None)
async def get_company(vacancy_id: int, session: SessionDep) -> Company | None:
    """What is stored, with no request to the source; null until looked up."""
    employer = await repository.employer_of(session, vacancy_id)
    return None if employer is None else Company.model_validate(employer)


@router.post("/{vacancy_id}/company", response_model=Company)
async def look_up_company(vacancy_id: int) -> Company:
    """Fetch the employer page (and, for an old row, the vacancy page first).

    No `SessionDep`: `pipeline.lookup_employer` opens short sessions around
    its page fetches, so no pooled connection waits on hh. 404 means there is
    no employer to find, with the reason; 502 means the source failed.
    """
    try:
        employer = await pipeline.lookup_employer(vacancy_id)
    except pipeline.EmployerUnknown as e:
        raise HTTPException(404, str(e)) from None
    except Exception as e:  # noqa: BLE001 -- throttled, 5xx, layout change: all the source's
        log.warning("employer lookup failed for vacancy %d: %s", vacancy_id, e)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None
    return Company.model_validate(employer)
