"""The employer behind a vacancy: what is stored, the lookup, the identity and the recruiter search."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ... import company_research, llm, repository
from ...config import DEFAULT_PATH, load_settings
from ...db import SessionLocal
from ...models import Employer, EmployerIdentity, RecruiterSearch
from ..schemas import Company, CompanyIdentity, CompanyRecruiters

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vacancies", tags=["company"])


@router.get("/{vacancy_id}/company", response_model=Company | None)
async def get_company(vacancy_id: int) -> Company | None:
    """Free; null until looked up. No `SessionDep`, so two connections are never held at once."""
    recruiters = await _stored_recruiters(vacancy_id)
    async with SessionLocal() as session:
        employer = await repository.employer_of(session, vacancy_id)
        if employer is None:
            return None
        return await _company(session, employer, vacancy_id, recruiters)


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
    recruiters = await _stored_recruiters(vacancy_id)
    async with SessionLocal() as session:
        return await _company(session, employer, vacancy_id, recruiters)


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
    return _recruiters(row)


async def _stored_recruiters(vacancy_id: int) -> CompanyRecruiters | None:
    """None for no search or nothing to search for; optional on a free read, so it never fails the card."""
    try:
        model = load_settings(DEFAULT_PATH).model_for("recruiters")
        row = await company_research.stored_recruiters(vacancy_id, model)
    except (company_research.NotLookedUp, company_research.EmployerUnknown):
        return None
    except Exception as e:  # noqa: BLE001 -- a bad config.toml, a database hiccup
        log.warning("reading the recruiter search failed for vacancy %d: %s", vacancy_id, e)
        return None
    return None if row is None else _recruiters(row)


async def _company(
    session: AsyncSession,
    employer: Employer,
    vacancy_id: int,
    recruiters: CompanyRecruiters | None,
) -> Company:
    """Field by field, as three of them live in `raw`, where `model_validate` cannot see."""
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


def _recruiters(row: RecruiterSearch) -> CompanyRecruiters:
    """The stored answer, as is."""
    return CompanyRecruiters(**row.answer, created_at=row.created_at)
