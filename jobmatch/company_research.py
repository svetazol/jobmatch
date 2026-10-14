"""The on-click company operations (docs/company-research.md): a source or model plus the database."""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Any

import anyio

from . import matching, repository, research
from .db import SessionLocal
from .models import Employer, EmployerIdentity, RecruiterSearch, Vacancy
from .sources import SOURCES, EmployerGone, Source, VacancyGone

log = logging.getLogger(__name__)


class EmployerUnknown(LookupError):
    """A dead end, not a failure (anonymous, gone, no employer pages); the API answers 404."""


class NothingToSearchFor(EmployerUnknown):
    """Looked up, but with neither a website nor a name to search for; the API answers 422."""


class NotLookedUp(LookupError):
    """Not a dead end: `lookup_employer` first, then ask again; the API answers 409."""


async def lookup_employer(vacancy_id: int) -> Employer:
    """Fetch and store the employer page; no session is held across the fetch, as in `pipeline._fetch`."""
    async with SessionLocal() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
    if vacancy is None:
        raise EmployerUnknown(f"no vacancy {vacancy_id}")
    source = SOURCES.get(vacancy.source)
    if source is None or source.fetch_employer is None:
        raise EmployerUnknown(f"{vacancy.source} has no employer pages")

    employer_id = vacancy.employer_external_id or await _employer_id_from_page(source, vacancy)
    try:
        data = await source.fetch_employer(employer_id)
    except EmployerGone:
        raise EmployerUnknown(f"{source.name} has no page for employer {employer_id}") from None
    async with SessionLocal.begin() as session:
        employer = await repository.upsert_employer(session, source.name, data)
    log.info("looked up employer %s for vacancy %d: %s", employer_id, vacancy_id, data.name)
    return employer


async def _employer_id_from_page(source: Source, vacancy: Vacancy) -> str:
    """For a row fetched before the column existed; an anonymous posting lands here on every click too."""
    try:
        data = await source.fetch(vacancy.url)
    except VacancyGone:
        async with SessionLocal.begin() as session:
            await repository.mark_delisted(session, vacancy.id)
        raise EmployerUnknown("the posting has been removed, so its employer is unknown") from None
    if data.employer_id is None:
        raise EmployerUnknown("this posting does not name its employer")
    async with SessionLocal.begin() as session:
        await repository.set_employer_external_id(session, vacancy.id, data.employer_id)
    return data.employer_id


async def identify_employer(vacancy_id: int, model: str) -> EmployerIdentity:
    """The stored answer to this exact request, else a paid one, checked and saved."""
    async with SessionLocal() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy is None:
            raise EmployerUnknown(f"no vacancy {vacancy_id}")
        employer = await repository.employer_of(session, vacancy_id)
        if employer is None:
            raise NotLookedUp("the company has not been looked up yet; look it up first")
        poster = poster_of(employer, vacancy)
        request = research.employer_request(poster, model)
        fingerprint = research.fingerprint(request)
        if (stored := await repository.identity_by_fingerprint(session, fingerprint)) is not None:
            return stored

    identity, call_id = await research.ask_employer(request, vacancy_id=vacancy_id)
    # Paid for: shielded, so a closed tab cannot lose the answer and make the next click pay again.
    with anyio.CancelScope(shield=True):
        homepage_ok = await _answers(identity.homepage)
        careers_ok = await _answers(identity.careers_page)
        async with SessionLocal.begin() as session:
            row = await repository.save_identity(
                session, vacancy_id, fingerprint, identity.model_dump(),
                homepage_ok, careers_ok, call_id,
            )
    log.info("identified the employer of vacancy %d: %s", vacancy_id, identity.employer)
    return row


async def _answers(url: str | None) -> bool | None:
    """None when there is no URL."""
    return None if url is None else await research.reachable(url)


def poster_of(employer: Employer, vacancy: Vacancy) -> research.Poster:
    """The hh employer page, and the posting exactly as Jev reads it."""
    about = "; ".join(v for key in ("city", "industries") if (v := employer.raw.get(key)))
    return research.Poster(
        name=employer.name,
        site_url=employer.site_url,
        about=about or None,
        posting=matching.job_text(vacancy),
    )


def target_of(employer: Employer, identity: EmployerIdentity | None) -> research.Target:
    """The identity's homepage only if it answered, as a made-up one would search the wrong company."""
    answer = identity.answer if identity is not None else {}
    homepage = answer.get("homepage") if identity is not None and identity.homepage_ok else None
    return research.Target(
        name=answer.get("employer") or employer.name,
        homepage=homepage or employer.site_url,
        city=employer.raw.get("city"),
    )


# Recruiters change jobs, so the same request is worth paying for again after this.
RECRUITERS_FRESH = dt.timedelta(days=30)


async def find_recruiters(
    vacancy_id: int, model: str, *, refresh: bool = False
) -> RecruiterSearch:
    """The stored search if fresh, else a paid one; `refresh` always pays (the user, 2026-10-14)."""
    employer_id, target, request, fingerprint, stored = await _recruiters_request(vacancy_id, model)
    if (not refresh and stored is not None
            and dt.datetime.now(dt.UTC) - stored.created_at < RECRUITERS_FRESH):
        return stored

    confirmed, call_id = await research.ask_recruiters(request, vacancy_id=vacancy_id)
    # Paid for: shielded, as in `identify_employer`.
    with anyio.CancelScope(shield=True):
        async with SessionLocal.begin() as session:
            row = await repository.save_recruiters(
                session, employer_id, fingerprint, dataclasses.asdict(confirmed), call_id
            )
    log.info(
        "searched recruiters at %s for vacancy %d: %d found, %d dropped",
        target.name or target.homepage, vacancy_id, len(confirmed.people), confirmed.dropped,
    )
    return row


async def stored_recruiters(vacancy_id: int, model: str) -> RecruiterSearch | None:
    """The search a request would reuse, fresh or not; never pays."""
    *_, stored = await _recruiters_request(vacancy_id, model)
    return stored


async def _recruiters_request(
    vacancy_id: int, model: str
) -> tuple[int, research.Target, dict[str, Any], str, RecruiterSearch | None]:
    """The employer, target, request, fingerprint and newest stored search; raises as `identify_employer`."""
    async with SessionLocal() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy is None:
            raise EmployerUnknown(f"no vacancy {vacancy_id}")
        employer = await repository.employer_of(session, vacancy_id)
        if employer is None:
            raise NotLookedUp("the company has not been looked up yet; look it up first")
        target = target_of(employer, await repository.latest_identity(session, vacancy_id))
        if not target.searchable:
            raise NothingToSearchFor("the company has neither a website nor a name to search for")
        request = research.recruiters_request(target, model)
        fingerprint = research.fingerprint(request)
        stored = await repository.latest_recruiters(session, employer.id, fingerprint)
    return employer.id, target, request, fingerprint, stored
