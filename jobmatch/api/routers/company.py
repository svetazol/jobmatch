"""The employer behind a vacancy: the lookup, the identity, the recruiter search and the written mark."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ... import company_research, llm, repository, research
from ...config import DEFAULT_PATH, load_settings
from ...db import SessionLocal
from ...models import Employer, EmployerIdentity, RecruiterSearch
from ..deps import get_session
from ..schemas import Company, CompanyIdentity, CompanyRecruiters, RecruiterWritten

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vacancies", tags=["company"])
# The mark belongs to a person, not a posting, so it is not under /api/vacancies.
recruiters_router = APIRouter(prefix="/api/recruiters", tags=["company"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{vacancy_id}/company", response_model=Company | None)
async def get_company(vacancy_id: int) -> Company | None:
    """Free; null until looked up. No `SessionDep`, so two connections are never held at once."""
    search = await _stored_recruiters(vacancy_id)
    async with SessionLocal() as session:
        employer = await repository.employer_of(session, vacancy_id)
        if employer is None:
            return None
        return await _company(session, employer, vacancy_id, search)


@router.post("/{vacancy_id}/company", response_model=Company)
async def look_up_company(vacancy_id: int) -> Company:
    """No `SessionDep`, so no pooled connection waits on hh. 404: no employer; 502: the source failed."""
    try:
        employer = await company_research.lookup_employer(vacancy_id)
    except company_research.EmployerUnknown as e:
        raise HTTPException(404, str(e)) from None
    except Exception as e:  # noqa: BLE001 -- throttled, 5xx, layout change: all the source's
        log.warning("employer lookup failed for vacancy %d: %s", vacancy_id, e)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None
    search = await _stored_recruiters(vacancy_id)
    async with SessionLocal() as session:
        return await _company(session, employer, vacancy_id, search)


@router.post("/{vacancy_id}/company/identity", response_model=CompanyIdentity)
async def identify_company(vacancy_id: int) -> CompanyIdentity:
    """409: look up first; 503: no API key; 502: the call failed, recorded in `llm_calls`."""
    # Checked here, so a missing key reads as setup (503), not the model failing (502).
    try:
        llm.api_key("to identify employers")
    except ValueError as e:
        raise HTTPException(503, str(e)) from None
    model = load_settings(DEFAULT_PATH).model_for("employer")
    try:
        row = await company_research.identify_employer(vacancy_id, model)
    except company_research.NotLookedUp:
        raise HTTPException(409, "look up the company first") from None
    except company_research.EmployerUnknown as e:
        raise HTTPException(404, str(e)) from None
    except Exception as e:  # noqa: BLE001 -- timeout, refusal, invalid JSON, OpenRouter's error
        log.warning("identifying the employer failed for vacancy %d: %s", vacancy_id, e)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None
    async with SessionLocal() as session:
        return await _identity(session, row)


@router.post("/{vacancy_id}/company/recruiters", response_model=CompanyRecruiters)
async def search_recruiters(vacancy_id: int, refresh: bool = False) -> CompanyRecruiters:
    """Free while fresh unless `?refresh=true`. 409, 503, 502 as above; 422: nothing to search for."""
    try:
        llm.api_key("to search for recruiters")
    except ValueError as e:     # 503 rather than 502, as above
        raise HTTPException(503, str(e)) from None
    model = load_settings(DEFAULT_PATH).model_for("recruiters")
    try:
        row = await company_research.find_recruiters(vacancy_id, model, refresh=refresh)
    except company_research.NotLookedUp:
        raise HTTPException(409, "look up the company first") from None
    except company_research.NothingToSearchFor as e:   # before EmployerUnknown: it is one
        raise HTTPException(422, str(e)) from None
    except company_research.EmployerUnknown as e:
        raise HTTPException(404, str(e)) from None
    except Exception as e:  # noqa: BLE001 -- timeout, refusal, invalid JSON, OpenRouter's error
        log.warning("searching for recruiters failed for vacancy %d: %s", vacancy_id, e)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None
    async with SessionLocal() as session:
        return await _recruiters(session, row)


@recruiters_router.patch("/written", status_code=204)
async def mark_written(body: RecruiterWritten, session: SessionDep) -> None:
    """Mark or unmark that the user wrote to someone; 422 for a URL that is not a LinkedIn profile."""
    url = research.linkedin_profile_url(body.linkedin_url)
    if url is None:
        raise HTTPException(422, "not a LinkedIn profile URL (linkedin.com/in/...)")
    if body.written:
        await repository.mark_written(session, url)
    else:
        await repository.unmark_written(session, url)
    await session.commit()


async def _stored_recruiters(vacancy_id: int) -> RecruiterSearch | None:
    """None for no search or nothing to search for; optional on a free read, so it never fails the card."""
    try:
        model = load_settings(DEFAULT_PATH).model_for("recruiters")
        return await company_research.stored_recruiters(vacancy_id, model)
    except (company_research.NotLookedUp, company_research.EmployerUnknown):
        return None
    except Exception as e:  # noqa: BLE001 -- a bad config.toml, a database hiccup
        log.warning("reading the recruiter search failed for vacancy %d: %s", vacancy_id, e)
        return None


async def _company(
    session: AsyncSession,
    employer: Employer,
    vacancy_id: int,
    search: RecruiterSearch | None,
) -> Company:
    """Field by field, as three live in `raw`; recruiters are optional, so a failed read is null."""
    row = await repository.latest_identity(session, vacancy_id)
    identity = None if row is None else await _identity(session, row)
    recruiters = None
    if search is not None:
        try:
            recruiters = await _recruiters(session, search)
        except Exception as e:  # noqa: BLE001 -- a database hiccup, a malformed stored answer
            log.warning("reading the written marks failed for vacancy %d: %s", vacancy_id, e)
    raw = employer.raw or {}
    return Company(
        name=employer.name,
        url=employer.url,
        site_url=employer.site_url,
        city=raw.get("city"),
        category=raw.get("category"),
        industries=raw.get("industries"),
        looked_up_at=employer.looked_up_at,
        identity=identity,
        recruiters=recruiters,
    )


async def _identity(session: AsyncSession, row: EmployerIdentity) -> CompanyIdentity:
    """The stored answer, with what its ledger row says it cost."""
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


async def _recruiters(session: AsyncSession, row: RecruiterSearch) -> CompanyRecruiters:
    """The stored answer, each person with when the user wrote to them, looked up canonically."""
    people = row.answer["people"]
    urls = [research.linkedin_profile_url(person["linkedin_url"]) for person in people]
    written = await repository.written_dates(session, [url for url in urls if url])
    people = [
        {**person, "written_at": written.get(url) if url else None}
        for person, url in zip(people, urls, strict=True)
    ]
    return CompanyRecruiters(**row.answer | {"people": people}, created_at=row.created_at)
