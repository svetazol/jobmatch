"""The recruiter search, kept: `recruiter_searches`, `pipeline.target_of` and
`pipeline.find_recruiters` on the real database, with `research.ask_recruiters`
stubbed -- nothing here reaches OpenRouter. The call itself and `keep_cited`
are tested in `test_research.py`.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal

import anyio
import pytest
from sqlalchemy import delete, select, update

from jobmatch import cli, pipeline, repository, research
from jobmatch.db import SessionLocal
from jobmatch.models import Employer, EmployerIdentity, LlmCall, RecruiterSearch, Vacancy

SOURCE_NAME = "test-recruiters"
MODEL = "google/gemini-2.5-flash-lite:online"

pytestmark = pytest.mark.anyio

CONFIRMED = research.Confirmed(
    people=(research.ConfirmedPerson(
        name="Иван Петров", role="IT Recruiter",
        linkedin_url="https://www.linkedin.com/in/ivan-petrov-123",
        evidence="Иван Петров – IT Recruiter – ЭР-Телеком | LinkedIn",
        source_title="Иван Петров – IT Recruiter – ЭР-Телеком",
    ),),
    dropped=1,
    sources=4,
)
IDENTITY = {
    "employer": "ЭР-Телеком", "homepage": "https://ertelecom.ru",
    "careers_page": "http://rabotavdomru.ru", "is_agency": False, "client": None,
    "notes": "",
}


@pytest.fixture
async def vacancy_id():
    """A fetched vacancy whose employer has been looked up; no identity yet."""
    async with SessionLocal.begin() as session:
        session.add(Employer(
            source=SOURCE_NAME, external_id="e-1", url="https://example.invalid/employer/e-1",
            name="ЭР-Телеком Холдинг", site_url="http://rabotavdomru.ru",
            raw={"city": "Пермь", "industries": "Телекоммуникации"},
        ))
        vacancy = Vacancy(
            source=SOURCE_NAME, external_id="v-1", url="https://example.invalid/v-1",
            title="Python developer", company="ЭР-Телеком Холдинг",
            description="Мы пишем биллинг.", employer_external_id="e-1",
            fetched_at=dt.datetime.now(dt.UTC),
        )
        session.add(vacancy)
        await session.flush()
        made = vacancy.id
    yield made
    async with SessionLocal.begin() as session:
        vacancies = select(Vacancy.id).where(Vacancy.source == SOURCE_NAME)
        employers = select(Employer.id).where(Employer.source == SOURCE_NAME)
        await session.execute(
            delete(RecruiterSearch).where(RecruiterSearch.employer_id.in_(employers))
        )
        await session.execute(
            delete(EmployerIdentity).where(EmployerIdentity.vacancy_id.in_(vacancies))
        )
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(vacancies)))
        await session.execute(delete(Employer).where(Employer.source == SOURCE_NAME))
        await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))


@pytest.fixture
def searches(monkeypatch):
    """`research.ask_recruiters` replaced; returns the (target, model) pairs asked.

    The fake still writes its ledger row, because `recruiter_searches` points
    at one.
    """
    asked: list[tuple[research.Target, str]] = []

    async def ask(target, model, *, vacancy_id, http_client=None):
        asked.append((target, model))
        call_id = await repository.record_call(
            kind="recruiters", vacancy_id=vacancy_id, model_requested=model,
            inputs_fingerprint=key(target, model), status="ok",
            cost_usd=Decimal("0.02"), started_at=dt.datetime.now(dt.UTC),
        )
        return CONFIRMED, call_id

    monkeypatch.setattr(research, "ask_recruiters", ask)
    return asked


def key(target: research.Target, model: str) -> str:
    return research.fingerprint(research.recruiters_request(target, model))


async def _employer_id() -> int:
    async with SessionLocal() as session:
        return await session.scalar(select(Employer.id).where(
            Employer.source == SOURCE_NAME, Employer.external_id == "e-1"
        ))


async def _another_posting(employer_external_id: str = "e-1") -> int:
    """A second vacancy, of the fixture's company unless told otherwise; an
    employer named here that does not exist is made as a twin of the first."""
    async with SessionLocal.begin() as session:
        if employer_external_id != "e-1":
            session.add(Employer(
                source=SOURCE_NAME, external_id=employer_external_id,
                url=f"https://example.invalid/employer/{employer_external_id}",
                name="ЭР-Телеком Холдинг", site_url="http://rabotavdomru.ru",
                raw={"city": "Пермь"},
            ))
        vacancy = Vacancy(
            source=SOURCE_NAME, external_id=f"v-{employer_external_id}-2",
            url=f"https://example.invalid/v-{employer_external_id}-2",
            title="Backend developer", company="ЭР-Телеком Холдинг",
            description="Мы пишем ещё один биллинг.",
            employer_external_id=employer_external_id, fetched_at=dt.datetime.now(dt.UTC),
        )
        session.add(vacancy)
        await session.flush()
        return vacancy.id


async def _searches() -> list[RecruiterSearch]:
    async with SessionLocal() as session:
        return list(await session.scalars(
            select(RecruiterSearch)
            .where(RecruiterSearch.employer_id == await _employer_id())
            .order_by(RecruiterSearch.id)
        ))


async def _identify(vacancy_id: int, homepage_ok: bool | None) -> None:
    """An identity row as `identify_employer` would leave it, unpaid."""
    call_id = await repository.record_call(
        kind="employer", vacancy_id=vacancy_id, model_requested="m",
        inputs_fingerprint="i" * 64, status="ok", started_at=dt.datetime.now(dt.UTC),
    )
    async with SessionLocal.begin() as session:
        await repository.save_identity(
            session, vacancy_id, "i" * 64, IDENTITY, homepage_ok, None, call_id
        )


# --- the table ---------------------------------------------------------------------

async def test_a_search_round_trips_and_the_newest_is_latest(vacancy_id):
    employer_id = await _employer_id()
    call_id = await repository.record_call(
        kind="recruiters", vacancy_id=vacancy_id, model_requested=MODEL,
        inputs_fingerprint="a" * 64, status="ok", started_at=dt.datetime.now(dt.UTC),
    )
    answer = dataclasses.asdict(CONFIRMED)
    async with SessionLocal.begin() as session:
        first = await repository.save_recruiters(
            session, employer_id, "a" * 64, answer, call_id
        )
    async with SessionLocal.begin() as session:
        # the same fingerprint again is allowed: a stale search is redone as is
        second = await repository.save_recruiters(
            session, employer_id, "a" * 64, {**answer, "dropped": 0}, call_id
        )

    assert first.created_at is not None and second.id != first.id
    async with SessionLocal() as session:
        latest = await repository.latest_recruiters(session, employer_id, "a" * 64)
        other = await repository.latest_recruiters(session, employer_id, "b" * 64)
    assert latest.id == second.id
    assert other is None
    assert first.answer["people"][0]["linkedin_url"] == CONFIRMED.people[0].linkedin_url


async def test_an_employer_never_searched_has_no_latest(vacancy_id):
    async with SessionLocal() as session:
        assert await repository.latest_recruiters(session, await _employer_id(), "a" * 64) is None


# --- the target --------------------------------------------------------------------

EMPLOYER = Employer(
    name="ЭР-Телеком Холдинг", site_url="http://rabotavdomru.ru",
    raw={"city": "Пермь", "industries": "Телекоммуникации"},
)


def identity(homepage_ok: bool | None, **answer) -> EmployerIdentity:
    return EmployerIdentity(answer={**IDENTITY, **answer}, homepage_ok=homepage_ok)


def test_without_an_identity_the_target_is_the_hh_page():
    assert pipeline.target_of(EMPLOYER, None) == research.Target(
        name="ЭР-Телеком Холдинг", homepage="http://rabotavdomru.ru", city="Пермь",
    )


def test_an_identity_names_the_target_and_gives_the_homepage_that_answered():
    assert pipeline.target_of(EMPLOYER, identity(True)) == research.Target(
        name="ЭР-Телеком", homepage="https://ertelecom.ru", city="Пермь",
    )


@pytest.mark.parametrize("homepage_ok", [False, None])
def test_a_homepage_that_did_not_answer_falls_back_to_the_hh_link(homepage_ok):
    target = pipeline.target_of(EMPLOYER, identity(homepage_ok))
    assert (target.name, target.homepage) == ("ЭР-Телеком", "http://rabotavdomru.ru")


def test_an_unknown_name_falls_back_to_the_hh_name():
    target = pipeline.target_of(EMPLOYER, identity(True, employer=None))
    assert target.name == "ЭР-Телеком Холдинг"


# --- the pipeline: freshness, on the real database ---------------------------------------

async def test_a_search_is_saved_as_the_confirmed_people(vacancy_id, searches):
    row = await pipeline.find_recruiters(vacancy_id, MODEL)

    ((target, _),) = searches
    # as JSON: the people tuple comes back a list
    assert row.answer == json.loads(json.dumps(dataclasses.asdict(CONFIRMED)))
    assert row.employer_id == await _employer_id()
    assert row.inputs_fingerprint == key(target, MODEL)
    assert [s.id for s in await _searches()] == [row.id]


async def test_the_search_uses_the_identity_when_there_is_one(vacancy_id, searches):
    await _identify(vacancy_id, homepage_ok=True)
    await pipeline.find_recruiters(vacancy_id, MODEL)

    ((target, _),) = searches
    assert target == research.Target("ЭР-Телеком", "https://ertelecom.ru", "Пермь")


async def test_a_fresh_search_with_the_same_request_is_free(vacancy_id, searches):
    first = await pipeline.find_recruiters(vacancy_id, MODEL)
    again = await pipeline.find_recruiters(vacancy_id, MODEL)

    assert again.id == first.id
    assert len(searches) == 1


async def test_a_search_older_than_fresh_is_asked_again(vacancy_id, searches):
    first = await pipeline.find_recruiters(vacancy_id, MODEL)
    async with SessionLocal.begin() as session:
        await session.execute(
            update(RecruiterSearch).where(RecruiterSearch.id == first.id)
            .values(created_at=RecruiterSearch.created_at - pipeline.RECRUITERS_FRESH)
        )
    again = await pipeline.find_recruiters(vacancy_id, MODEL)

    assert again.id != first.id
    assert again.inputs_fingerprint == first.inputs_fingerprint
    assert len(searches) == 2
    assert [s.id for s in await _searches()] == [first.id, again.id]


async def test_two_postings_with_different_requests_each_pay_once(vacancy_id, searches):
    """One identified, one not: two requests, two searches, then both free --
    not a new payment on every switch between them (the user's rule, 2026-10-14)."""
    other = await _another_posting()
    await _identify(vacancy_id, homepage_ok=True)

    for posting in (vacancy_id, other, vacancy_id, other):
        await pipeline.find_recruiters(posting, MODEL)

    assert len(searches) == 2
    assert len(await _searches()) == 2


async def test_two_postings_with_the_same_request_share_a_search(vacancy_id, searches):
    other = await _another_posting()
    first = await pipeline.find_recruiters(vacancy_id, MODEL)

    assert (await pipeline.find_recruiters(other, MODEL)).id == first.id
    assert len(searches) == 1


async def test_twin_employers_do_not_share_a_search(vacancy_id, searches):
    """Same name, link and city, so the same request -- but another company."""
    twin = await _another_posting("e-2")
    first = await pipeline.find_recruiters(vacancy_id, MODEL)
    second = await pipeline.find_recruiters(twin, MODEL)

    assert second.inputs_fingerprint == first.inputs_fingerprint
    assert second.employer_id != first.employer_id
    assert len(searches) == 2


async def test_the_stored_search_is_read_without_paying(vacancy_id, searches):
    assert await pipeline.stored_recruiters(vacancy_id, MODEL) is None
    row = await pipeline.find_recruiters(vacancy_id, MODEL)

    assert (await pipeline.stored_recruiters(vacancy_id, MODEL)).id == row.id
    assert await pipeline.stored_recruiters(vacancy_id, "perplexity/sonar") is None
    assert len(searches) == 1


async def test_another_model_is_another_request(vacancy_id, searches):
    first = await pipeline.find_recruiters(vacancy_id, MODEL)
    other = await pipeline.find_recruiters(vacancy_id, "perplexity/sonar")

    assert other.id != first.id and other.inputs_fingerprint != first.inputs_fingerprint
    assert len(searches) == 2


async def test_a_new_identity_changes_the_request(vacancy_id, searches):
    """The identity sharpens the target, so a search made before it is redone."""
    first = await pipeline.find_recruiters(vacancy_id, MODEL)
    await _identify(vacancy_id, homepage_ok=True)
    again = await pipeline.find_recruiters(vacancy_id, MODEL)

    assert again.id != first.id
    assert len(searches) == 2


async def test_without_a_looked_up_employer_there_is_nothing_to_search(vacancy_id, searches):
    async with SessionLocal.begin() as session:
        await session.execute(delete(Employer).where(Employer.source == SOURCE_NAME))

    with pytest.raises(pipeline.NotLookedUp, match="looked up"):
        await pipeline.find_recruiters(vacancy_id, MODEL)
    assert searches == []


async def test_a_company_with_no_name_is_not_searched(vacancy_id, searches):
    async with SessionLocal.begin() as session:
        await session.execute(
            update(Employer).where(Employer.source == SOURCE_NAME).values(name=None)
        )

    with pytest.raises(pipeline.EmployerUnknown, match="no name"):
        await pipeline.find_recruiters(vacancy_id, MODEL)
    assert searches == []


async def test_a_failed_search_raises_and_stores_nothing(vacancy_id, monkeypatch):
    async def fails(target, model, *, vacancy_id, http_client=None):
        raise RuntimeError("OpenRouter said no")

    monkeypatch.setattr(research, "ask_recruiters", fails)
    with pytest.raises(RuntimeError, match="said no"):
        await pipeline.find_recruiters(vacancy_id, MODEL)
    assert await _searches() == []


async def test_an_unknown_vacancy_is_employer_unknown(searches):
    with pytest.raises(pipeline.EmployerUnknown, match="no vacancy"):
        await pipeline.find_recruiters(0, MODEL)


async def test_a_search_paid_for_is_saved_even_if_the_click_is_cancelled(
    vacancy_id, searches, monkeypatch
):
    """A closed tab cancels the request mid-save; the next click must not pay."""
    saving = anyio.Event()
    save = repository.save_recruiters

    async def slow_save(session, *args):
        saving.set()
        await anyio.sleep(0.1)
        return await save(session, *args)

    monkeypatch.setattr(repository, "save_recruiters", slow_save)
    async with anyio.create_task_group() as tg:
        tg.start_soon(pipeline.find_recruiters, vacancy_id, MODEL)
        await saving.wait()
        tg.cancel_scope.cancel()

    assert len(await _searches()) == 1
    await pipeline.find_recruiters(vacancy_id, MODEL)
    assert len(searches) == 1


# --- the command -------------------------------------------------------------------

async def test_the_command_prints_the_people_and_whether_it_paid(vacancy_id, searches, capsys):
    assert await cli.cmd_recruiters("config.toml", vacancy_id) == 0
    out = capsys.readouterr().out
    assert ("Иван Петров · IT Recruiter · https://www.linkedin.com/in/ivan-petrov-123 · "
            "Иван Петров – IT Recruiter – ЭР-Телеком") in out
    assert "1 found · 1 dropped · 4 sources" in out
    assert "paid: llm_calls #" in out

    assert await cli.cmd_recruiters("config.toml", vacancy_id) == 0
    assert "cached, free: searched earlier by llm_calls #" in capsys.readouterr().out


async def test_the_command_reports_why_it_cannot_search(vacancy_id, monkeypatch, capsys):
    async def fails(*args):
        raise pipeline.NotLookedUp("the company has not been looked up yet")

    monkeypatch.setattr(pipeline, "find_recruiters", fails)
    assert await cli.cmd_recruiters("config.toml", vacancy_id) == 1
    assert "has not been looked up" in capsys.readouterr().out
