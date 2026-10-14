"""The on-click company operations of `docs/company-research.md`: they need a source or a
model and the database at once, like `pipeline`, but answer a click rather than run a phase."""
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
    """There is no employer to look up for this vacancy; the message says why.

    A dead end, not a failure: the posting is anonymous, gone, or from a site
    without employer pages. The API answers 404 with the message.
    """


class NothingToSearchFor(EmployerUnknown):
    """The company was looked up but has neither a website nor a name, so
    there is nothing to search the web for. A subclass so callers that only
    care about a dead end need not know it; the API tells it from a missing
    vacancy and answers 422.
    """


class NotLookedUp(LookupError):
    """The vacancy's company has not been looked up, so there is nothing to
    tell a model about it yet. Not a dead end: `lookup_employer` first, then
    ask again. The API answers 409.
    """


async def lookup_employer(vacancy_id: int) -> Employer:
    """Fetch the employer page behind a vacancy and store what it says.

    On demand from the UI, never from a run. A repeat call refreshes the row.
    Like `pipeline._fetch`, no session is held across a page fetch. Anything other than
    `EmployerUnknown` (throttled, a 5xx, a layout change) propagates.
    """
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
    """Re-read the vacancy page for the one thing a row fetched before
    `employer_external_id` existed is missing, and store only that.

    An anonymous posting also lands here on every click, since its NULL looks
    the same as an old row's -- one page per click, on a vacancy someone chose
    to open.
    """
    try:
        data = await source.fetch(vacancy.url)
    except VacancyGone:
        async with SessionLocal.begin() as session:
            # a positive signal, as in pipeline._fetch
            await repository.mark_delisted(session, vacancy.id)
        raise EmployerUnknown("the posting has been removed, so its employer is unknown") from None
    if data.employer_id is None:
        raise EmployerUnknown("this posting does not name its employer")
    async with SessionLocal.begin() as session:
        await repository.set_employer_external_id(session, vacancy.id, data.employer_id)
    return data.employer_id


async def identify_employer(vacancy_id: int, model: str) -> EmployerIdentity:
    """Who is behind a posting: the stored answer to this exact request, or a
    paid one (`research.ask_employer`), its homepage checked and the answer saved.

    Needs the employer row `lookup_employer` writes -- its name and link are
    half of what the model is told -- and raises `NotLookedUp` without it. A repeat click with nothing changed is
    free; an edited posting, a re-looked-up employer page or another model is
    a new request and pays. As in `pipeline._fetch`, no session is held across a
    network call. Failures of the call propagate, already in `llm_calls`.
    """
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
    # Paid for from here on: a cancelled request (a closed tab) must not lose
    # the answer, or the next click pays for it again. Shielded, and bounded by
    # the checks' own deadlines -- one after the other, two at most.
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
    """Whether a URL from the answer responds; None when there is no URL."""
    return None if url is None else await research.reachable(url)


def poster_of(employer: Employer, vacancy: Vacancy) -> research.Poster:
    """What the model is told: the hh employer page, and the posting exactly as
    Jev reads it. Only `job_text` is shared -- Jev's request and fingerprint
    are untouched."""
    about = "; ".join(v for key in ("city", "industries") if (v := employer.raw.get(key)))
    return research.Poster(
        name=employer.name,
        site_url=employer.site_url,
        about=about or None,
        posting=matching.job_text(vacancy),
    )


def target_of(employer: Employer, identity: EmployerIdentity | None) -> research.Target:
    """The company to search for recruiters at: the identity's answer where it
    has one, else the hh employer page.

    The identity's homepage only if it answered (`homepage_ok`): a homepage
    the model made up would point the search at the wrong company. Without
    it the website is hh's link, often a careers or landing host
    (rabotavdomru.ru for ЭР-Телеком, l.tbank.ru for Т-Банк), and the search
    names the company by that host. For an
    agency posting the identity's employer is the agency, so the search is for
    the agency's recruiters -- the people who posted the vacancy -- not the
    client's, who is often unnamed.
    """
    answer = identity.answer if identity is not None else {}
    homepage = answer.get("homepage") if identity is not None and identity.homepage_ok else None
    return research.Target(
        name=answer.get("employer") or employer.name,
        homepage=homepage or employer.site_url,
        city=employer.raw.get("city"),
    )


# How long a stored search is shown instead of asked again. Recruiters change
# jobs, so the same request is worth paying for again after this. Not config:
# nobody tunes it per run. Outside the fingerprint, so an older search with
# the same inputs is redone without a new request shape.
RECRUITERS_FRESH = dt.timedelta(days=30)


async def find_recruiters(
    vacancy_id: int, model: str, *, refresh: bool = False
) -> RecruiterSearch:
    """Who recruits for the company behind a posting: the stored search for
    this request if it is younger than `RECRUITERS_FRESH`, else a paid one
    (`research.ask_recruiters`), saved. `refresh` always pays and saves a new
    row, fresh one or not: the user asked for it (2026-10-14), to redo a
    search they doubt without waiting out the month.

    Per employer and request: the request holds the company, not the posting,
    so postings of one company that make the same request share a search, and
    one never pays twice for a request (the user's rule, 2026-10-14). Needs
    `lookup_employer` first (`NotLookedUp`); the identity is optional and only
    sharpens the target. As in `identify_employer`, no session is held across
    the network call, and failures of the call propagate, already in
    `llm_calls`.
    """
    employer_id, target, request, fingerprint, stored = await _recruiters_request(vacancy_id, model)
    if (not refresh and stored is not None
            and dt.datetime.now(dt.UTC) - stored.created_at < RECRUITERS_FRESH):
        return stored

    confirmed, call_id = await research.ask_recruiters(request, vacancy_id=vacancy_id)
    # Paid for from here on: as in `identify_employer`, a cancelled request
    # must not lose the answer.
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
    """The search a posting's request would reuse, fresh or not; never pays.
    What the card shows before (or instead of) a click."""
    *_, stored = await _recruiters_request(vacancy_id, model)
    return stored


async def _recruiters_request(
    vacancy_id: int, model: str
) -> tuple[int, research.Target, dict[str, Any], str, RecruiterSearch | None]:
    """The employer, the target, the request and its fingerprint, and its newest
    stored search; raises as `identify_employer` does without a vacancy or a
    lookup, and `NothingToSearchFor` for a company with neither a website nor
    a name."""
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
