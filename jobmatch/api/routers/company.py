"""The employer behind a vacancy: read what is stored, or look it up on click,
ask a model who is behind the posting, and search for who recruits there."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ... import llm, pipeline, repository
from ...config import DEFAULT_PATH, load_settings
from ...db import SessionLocal
from ...models import Employer, EmployerIdentity, RecruiterSearch
from ..schemas import Company, CompanyIdentity, CompanyRecruiters

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vacancies", tags=["company"])

@router.get("/{vacancy_id}/company", response_model=Company | None)
async def get_company(vacancy_id: int) -> Company | None:
    """What is stored, with no request to the source or a model; null until
    looked up.

    No `SessionDep`: `pipeline.stored_recruiters` opens its own session, so it
    runs first and this one opens after, rather than holding two connections.
    """
    recruiters = await _stored_recruiters(vacancy_id)
    async with SessionLocal() as session:
        employer = await repository.employer_of(session, vacancy_id)
        if employer is None:
            return None
        return await _company(session, employer, vacancy_id, recruiters)


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
    recruiters = await _stored_recruiters(vacancy_id)
    async with SessionLocal() as session:
        return await _company(session, employer, vacancy_id, recruiters)


@router.post("/{vacancy_id}/company/identity", response_model=CompanyIdentity)
async def identify_company(vacancy_id: int) -> CompanyIdentity:
    """Ask `research_model` who is behind the posting; free when this exact
    request was answered before (`pipeline.identify_employer`).

    No `SessionDep`, as above: no pooled connection waits on the model. 409
    means the company must be looked up first, 503 that there is no API key,
    502 that the call failed -- it is in `llm_calls` either way.
    """
    # Checked here, not left to `research.ask_employer`, so a missing key reads as the
    # server's setup (503) rather than the model failing (502).
    try:
        llm.api_key("to identify employers")
    except ValueError as e:
        raise HTTPException(503, str(e)) from None
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


@router.post("/{vacancy_id}/company/recruiters", response_model=CompanyRecruiters)
async def search_recruiters(vacancy_id: int, refresh: bool = False) -> CompanyRecruiters:
    """Ask `people_model` to search the web for who recruits at the company;
    free when the same request was answered within `pipeline.RECRUITERS_FRESH`,
    unless `?refresh=true`, which always pays.

    No `SessionDep`, as above. 409 means the company must be looked up first,
    422 that it has neither a website nor a name to search for, 503 that
    there is no API key, 502 that the call failed -- it is in `llm_calls`
    either way.
    """
    try:
        llm.api_key("to search for recruiters")
    except ValueError as e:     # 503 rather than 502, as above
        raise HTTPException(503, str(e)) from None
    model = load_settings(DEFAULT_PATH).people_model
    try:
        row = await pipeline.find_recruiters(vacancy_id, model, refresh=refresh)
    except pipeline.NotLookedUp:
        raise HTTPException(409, "look up the company first") from None
    except pipeline.NothingToSearchFor as e:   # before EmployerUnknown: it is one
        raise HTTPException(422, str(e)) from None
    except pipeline.EmployerUnknown as e:
        raise HTTPException(404, str(e)) from None
    except Exception as e:  # noqa: BLE001 -- timeout, refusal, invalid JSON, OpenRouter's error
        log.warning("searching for recruiters failed for vacancy %d: %s", vacancy_id, e)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None
    return _recruiters(row)


async def _stored_recruiters(vacancy_id: int) -> CompanyRecruiters | None:
    """The search the card would show; None when there is none, or nothing to
    search for yet (no lookup, no website or name) -- the POST says why when clicked.

    The section is optional on a free read, so no failure here fails the card.
    """
    try:
        model = load_settings(DEFAULT_PATH).people_model
        row = await pipeline.stored_recruiters(vacancy_id, model)
    except (pipeline.NotLookedUp, pipeline.EmployerUnknown):
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
    """The employer, with the newest identity asked for this vacancy and the
    recruiter search already read.

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
        recruiters=recruiters,
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


def _recruiters(row: RecruiterSearch) -> CompanyRecruiters:
    """The stored answer (`dataclasses.asdict(research.Confirmed)`), as is."""
    return CompanyRecruiters(**row.answer, created_at=row.created_at)
