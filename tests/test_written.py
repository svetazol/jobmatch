"""The written mark: `research.linkedin_profile_url`, `recruiter_contacts` and its
route on the real database, and the recruiter card that carries it. A stored
search stands in for a paid one; nothing here reaches OpenRouter.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import httpx2
import pytest
from sqlalchemy import delete, select

from jobmatch import company_research, llm, repository, research
from jobmatch.api.app import app
from jobmatch.config import DEFAULT_PATH, load_settings
from jobmatch.db import SessionLocal
from jobmatch.models import Employer, LlmCall, RecruiterContact, RecruiterSearch, Vacancy

SOURCE_NAME = "test-written"
# recruiter_contacts has no source column, so the test's rows are told apart by slug.
SLUG = "jobmatch-test-written"
ONE = f"https://www.linkedin.com/in/{SLUG}-one"
TWO = f"https://www.linkedin.com/in/{SLUG}-two"

pytestmark = pytest.mark.anyio


def person(url: str) -> research.ConfirmedPerson:
    return research.ConfirmedPerson(
        name="Анна", role="HR", linkedin_url=url, evidence="Анна – HR – Окко",
        source_title=None,
    )


ANSWER = dataclasses.asdict(research.Confirmed(
    # TWO is stored un-canonical, as an older or hand-made row could be.
    people=(person(ONE), person(f"https://ru.linkedin.com/in/{SLUG.title()}-Two/")),
    dropped=0, sources=2,
))


@pytest.fixture
async def no_marks():
    """Deletes the marks the test made."""
    yield
    async with SessionLocal.begin() as session:
        await session.execute(
            delete(RecruiterContact).where(RecruiterContact.linkedin_url.contains(SLUG))
        )


@pytest.fixture
async def vacancy_id():
    """A looked-up employer, a vacancy of it, and a stored search its request would reuse."""
    async with SessionLocal.begin() as session:
        session.add(Employer(
            source=SOURCE_NAME, external_id="e-1", url="https://example.invalid/employer/e-1",
            name="Окко", site_url="https://okko.tv/", raw={"city": "Санкт-Петербург"},
        ))
        vacancy = Vacancy(
            source=SOURCE_NAME, external_id="v-1", url="https://example.invalid/v-1",
            title="Python developer", company="Окко", description="Мы пишем плеер.",
            employer_external_id="e-1", fetched_at=dt.datetime.now(dt.UTC),
        )
        session.add(vacancy)
        await session.flush()
        made = vacancy.id
    await _save_search(made)
    yield made
    async with SessionLocal.begin() as session:
        vacancies = select(Vacancy.id).where(Vacancy.source == SOURCE_NAME)
        employers = select(Employer.id).where(Employer.source == SOURCE_NAME)
        await session.execute(
            delete(RecruiterSearch).where(RecruiterSearch.employer_id.in_(employers))
        )
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(vacancies)))
        await session.execute(delete(Employer).where(Employer.source == SOURCE_NAME))
        await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))


async def _save_search(vacancy_id: int) -> RecruiterSearch:
    """A search under the fingerprint the card's request has, with an unpaid ledger row."""
    async with SessionLocal() as session:
        employer = await repository.employer_of(session, vacancy_id)
    model = load_settings(DEFAULT_PATH).model_for("recruiters")
    fingerprint = research.fingerprint(
        research.recruiters_request(company_research.target_of(employer, None), model)
    )
    call_id = await repository.record_call(
        kind="recruiters", vacancy_id=vacancy_id, model_requested=model,
        inputs_fingerprint=fingerprint, status="ok", started_at=dt.datetime.now(dt.UTC),
    )
    async with SessionLocal.begin() as session:
        return await repository.save_recruiters(
            session, employer.id, fingerprint, ANSWER, call_id
        )


async def _mark(url: str) -> None:
    async with SessionLocal.begin() as session:
        await repository.mark_written(session, url)


async def _written(*urls: str) -> dict[str, dt.datetime]:
    async with SessionLocal() as session:
        return await repository.written_dates(session, urls)


# --- the canonical URL -------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/anna-k",
    "https://www.linkedin.com/in/anna-k/",
    "https://ru.linkedin.com/in/Anna-K",
    "http://linkedin.com/in/anna-k?trk=public_profile",
    "  https://www.linkedin.com/in/ANNA-K/details/experience/  ",
])
def test_every_form_of_a_profile_is_one_url(url):
    assert research.linkedin_profile_url(url) == "https://www.linkedin.com/in/anna-k"


def test_a_cyrillic_slug_is_quoted():
    assert research.linkedin_profile_url("https://www.linkedin.com/in/Анна") == (
        "https://www.linkedin.com/in/%D0%B0%D0%BD%D0%BD%D0%B0"
    )


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/company/okko",
    "https://example.com/in/anna-k",
    "linkedin.com/in/anna-k",
    "",
])
def test_anything_but_a_profile_has_no_url(url):
    assert research.linkedin_profile_url(url) is None


# --- the table ---------------------------------------------------------------------

async def test_marking_twice_keeps_the_first_date(no_marks):
    await _mark(ONE)
    first = (await _written(ONE))[ONE]
    await _mark(ONE)

    assert await _written(ONE) == {ONE: first}


async def test_unmarking_deletes_the_mark(no_marks):
    await _mark(ONE)
    async with SessionLocal.begin() as session:
        await repository.unmark_written(session, ONE)

    assert await _written(ONE) == {}


async def test_written_dates_holds_only_the_marked(no_marks):
    await _mark(ONE)

    assert set(await _written(ONE, TWO)) == {ONE}
    assert await _written() == {}


# --- the API -----------------------------------------------------------------------

@pytest.fixture
async def client(monkeypatch):
    """The app, with a key set and a search that fails loudly if anything tries to pay."""
    async def no_call(*args, **kwargs):
        raise AssertionError("a model was called")

    monkeypatch.setenv(llm.API_KEY_ENV, "test-key")
    monkeypatch.setattr(research, "ask_recruiters", no_call)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _patch(client, url: str, written: bool) -> httpx2.Response:
    return await client.patch(
        "/api/recruiters/written", json={"linkedin_url": url, "written": written}
    )


async def _people(client, vacancy_id: int) -> list[dict]:
    card = await client.get(f"/api/vacancies/{vacancy_id}/company")
    return card.json()["recruiters"]["people"]


async def test_a_canonical_and_a_messy_url_mark_one_person(client, no_marks):
    assert (await _patch(client, ONE, True)).status_code == 204
    messy = f"https://ru.linkedin.com/in/{SLUG}-ONE/?trk=x"
    assert (await _patch(client, messy, True)).status_code == 204

    async with SessionLocal() as session:
        rows = list(await session.scalars(
            select(RecruiterContact.linkedin_url).where(RecruiterContact.linkedin_url.contains(SLUG))
        ))
    assert rows == [ONE]


async def test_a_url_that_is_not_a_profile_is_422(client, no_marks):
    resp = await _patch(client, "https://www.linkedin.com/company/okko", True)

    assert resp.status_code == 422
    assert "LinkedIn profile" in resp.json()["detail"]


async def test_the_card_carries_the_mark_and_loses_it_on_undo(vacancy_id, client, no_marks):
    assert [p["written_at"] for p in await _people(client, vacancy_id)] == [None, None]

    await _patch(client, ONE, True)
    marked = await _people(client, vacancy_id)
    assert dt.datetime.fromisoformat(marked[0]["written_at"]) == (await _written(ONE))[ONE]
    assert marked[1]["written_at"] is None

    await _patch(client, ONE, False)
    assert [p["written_at"] for p in await _people(client, vacancy_id)] == [None, None]


async def test_a_stored_url_in_another_form_still_shows_its_mark(vacancy_id, client, no_marks):
    await _patch(client, TWO, True)

    assert [p["written_at"] is not None for p in await _people(client, vacancy_id)] == [False, True]


async def test_the_search_route_carries_the_mark_too(vacancy_id, client, no_marks):
    await _patch(client, ONE, True)

    resp = await client.post(f"/api/vacancies/{vacancy_id}/company/recruiters")

    assert resp.status_code == 200
    assert resp.json()["people"][0]["written_at"] is not None


async def test_a_new_search_keeps_the_mark(vacancy_id, client, no_marks):
    await _patch(client, ONE, True)
    newer = await _save_search(vacancy_id)

    card = (await client.get(f"/api/vacancies/{vacancy_id}/company")).json()["recruiters"]

    assert dt.datetime.fromisoformat(card["created_at"]) == newer.created_at
    assert card["people"][0]["written_at"] is not None


async def test_a_failed_read_of_the_marks_leaves_the_card_whole(
    vacancy_id, client, no_marks, monkeypatch
):
    async def fails(*args):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(repository, "written_dates", fails)
    resp = await client.get(f"/api/vacancies/{vacancy_id}/company")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Окко" and resp.json()["recruiters"] is None
