"""The only write path in the API: three nullable timestamps."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ... import repository
from ..deps import get_session
from ..schemas import TriagePatch

router = APIRouter(prefix="/api/vacancies", tags=["triage"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.patch("/{vacancy_id}/triage", status_code=204)
async def set_triage(vacancy_id: int, patch: TriagePatch, session: SessionDep) -> None:
    """Set or clear one of seen / starred / hidden.

    `false` clears the timestamp rather than storing a false — that is what
    Undo sends after a hide, and it is why these are nullable timestamps
    instead of booleans (§3.1): you get "when" for free and clearing is the
    same statement.
    """
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if len(fields) != 1:
        raise HTTPException(422, "set exactly one of seen, starred, hidden")

    field, on = next(iter(fields.items()))
    if not await repository.set_triage(session, vacancy_id, field, on):
        raise HTTPException(404, f"no vacancy {vacancy_id}")
    await session.commit()
