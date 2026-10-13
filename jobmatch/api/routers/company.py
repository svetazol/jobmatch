"""The employer behind a vacancy: read what is stored, or look it up on click,
and ask a model who is behind the posting."""
from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ... import identify, pipeline, repository
from ...config import DEFAULT_PATH, load_settings
from ...db import SessionLocal
from ...models import Employer, EmployerIdentity
from ..deps import get_session
from ..schemas import Company, CompanyIdentity

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vacancies", tags=["company"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{vacancy_id}/company", response_model=Company | None)
async def get_company(vacancy_id: int, session: SessionDep) -> Company | None:
    """What is stored, with no request to the source; null until looked up."""
    employer = await repository.employer_of(session, vacancy_id)
    return None if employer is None else await _company(session, employer, vacancy_id)


@router.post("/{vacancy_id}/company", response_model=Company)
async def look_up_company(vacancy_id: int) -> Company:
    """Fetch the employer page (and, for an old row, the vacancy page first).

    No `SessionDep`: `pipeline.lookup_employer` opens short sessions around
    its page fetches, so no pooled connection waits on hh. 404 means there is
    no employer to find, with the reason; 502 means the source failed. The
    identity is whatever is already stored: asking a model is its own request.
    """
    try:
        employer = await pipeline.lookup_employer(vacancy_id)
    except pipeline.EmployerUnknown as e:
        raise HTTPException(404, str(e)) from None
    except Exception as e:  # noqa: BLE001 -- throttled, 5xx, layout change: all the source's
        log.warning("employer lookup failed for vacancy %d: %s", vacancy_id, e)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None
    async with SessionLocal() as session:
        return await _company(session, employer, vacancy_id)


@router.post("/{vacancy_id}/company/identity", response_model=CompanyIdentity)
async def identify_company(vacancy_id: int) -> CompanyIdentity:
    """Ask `research_model` who is behind the posting; free when this exact
    request was answered before (`pipeline.identify_employer`).

    No `SessionDep`, as above: no pooled connection waits on the model. 409
    means the company must be looked up first, 503 that there is no API key,
    502 that the call failed -- it is in `llm_calls` either way.
    """
    # Checked here, not left to `identify.ask`, so a missing key reads as the
    # server's setup (503) rather than the model failing (502).
    if not os.environ.get(identify.API_KEY_ENV):
        raise HTTPException(503, f"Set {identify.API_KEY_ENV} in .env to identify employers.")
    model = load_settings(DEFAULT_PATH).research_model
    try:
        row = await pipeline.identify_employer(vacancy_id, model)
    except pipeline.NotLookedUp:
        raise HTTPException(409, "look up the company first") from None
    except pipeline.EmployerUnknown as e:
        raise HTTPException(404, str(e)) from None
    except Exception as e:  # noqa: BLE001 -- timeout, refusal, invalid JSON, OpenRouter's error
        log.warning("identifying the employer failed for vacancy %d: %s", vacancy_id, e)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None
    async with SessionLocal() as session:
        return await _identity(session, row)


async def _company(session: AsyncSession, employer: Employer, vacancy_id: int) -> Company:
    """The employer, with the newest identity asked for this vacancy.

    Written out field by field: three of them live in `raw`, which
    `model_validate` cannot see into.
    """
    row = await repository.latest_identity(session, vacancy_id)
    raw = employer.raw or {}
    return Company(
        name=employer.name,
        url=employer.url,
        site_url=employer.site_url,
        city=raw.get("city"),
        category=raw.get("category"),
        industries=raw.get("industries"),
        looked_up_at=employer.looked_up_at,
        identity=None if row is None else await _identity(session, row),
    )


async def _identity(session: AsyncSession, row: EmployerIdentity) -> CompanyIdentity:
    """The stored answer, flattened, with what its ledger row says it cost."""
    call = await repository.get_call(session, row.llm_call_id)   # an FK: always there
    return CompanyIdentity(
        **row.answer,
        homepage_ok=row.homepage_ok,
        careers_ok=row.careers_ok,
        model=call.model_resolved or call.model_requested,
        cost_usd=None if call.cost_usd is None else float(call.cost_usd),
        llm_call_id=call.id,
        created_at=row.created_at,
    )
