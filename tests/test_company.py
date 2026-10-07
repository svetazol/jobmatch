"""Looking up a vacancy's employer: a fake source, the real database, the API.

The source is fake so no test reaches hh; the database is real for the same
reason as everywhere else here -- the join on (source, external_id) and the
upsert's conflict target are the database's behaviour.
"""
import datetime as dt

import httpx2
import pytest
from sqlalchemy import delete, select

from jobmatch import repository
from jobmatch.api.app import app
from jobmatch.db import SessionLocal
from jobmatch.models import Employer, Vacancy
from jobmatch.sources import (
    SOURCES, EmployerData, EmployerGone, Source, VacancyData, VacancyGone,
)

SOURCE_NAME = "test-company"

pytestmark = pytest.mark.anyio


async def _no_discover(*args, **kwargs):
    return
    yield


def page(employer_id: str | None, description: str = "fresh text") -> VacancyData:
    return VacancyData(
        external_id="c-1", url="https://example.invalid/c-1", title="Role",
        description=description, company="Okko", employer_id=employer_id,
    )


def employer(employer_id: str = "77", name: str = "Okko") -> EmployerData:
    return EmployerData(
        external_id=employer_id, url=f"https://example.invalid/employer/{employer_id}",
        name=name, site_url="https://okko.tv/", raw={"city": "Санкт-Петербург"},
    )


@pytest.fixture
def source(monkeypatch):
    """Register a fake source; returns the list of calls it received."""
    calls: list[tuple[str, str]] = []

    def install(fetch=None, fetch_employer=None):
        async def _fetch(url):
            calls.append(("fetch", url))
            return fetch(url)

        async def _fetch_employer(employer_id):
            calls.append(("employer", employer_id))
            return fetch_employer(employer_id)

        monkeypatch.setitem(SOURCES, SOURCE_NAME, Source(
            name=SOURCE_NAME, hosts=("example.invalid",), discover=_no_discover,
            fetch=_fetch, fetch_employer=_fetch_employer,
        ))
        return calls

    return install


@pytest.fixture
async def vacancy():
    """One fetched vacancy with no employer id: a row from before the column."""
    async with SessionLocal.begin() as session:
        row = Vacancy(
            source=SOURCE_NAME, external_id="c-1", url="https://example.invalid/c-1",
            title="Role", company="Okko", description="stored text", content_hash="h" * 64,
            fetched_at=dt.datetime.now(dt.UTC),
        )
        session.add(row)
        await session.flush()
        vacancy_id = row.id

    yield vacancy_id

    async with SessionLocal.begin() as session:
        await session.execute(delete(Employer).where(Employer.source == SOURCE_NAME))
        await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))


@pytest.fixture
async def client():
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _vacancy(vacancy_id: int) -> Vacancy:
    async with SessionLocal() as session:
        return await session.get_one(Vacancy, vacancy_id)


async def _set_employer_id(vacancy_id: int, employer_id: str) -> None:
    async with SessionLocal.begin() as session:
        await repository.set_employer_external_id(session, vacancy_id, employer_id)


async def test_nothing_is_stored_until_someone_asks(client, vacancy):
    resp = await client.get(f"/api/vacancies/{vacancy}/company")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_lookup_with_a_known_id_fetches_only_the_employer_page(client, vacancy, source):
    await _set_employer_id(vacancy, "77")
    calls = source(fetch=lambda url: pytest.fail("the vacancy page is not needed"),
                   fetch_employer=lambda eid: employer(eid))

    resp = await client.post(f"/api/vacancies/{vacancy}/company")

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Okko"
    assert body["site_url"] == "https://okko.tv/"
    assert body["url"] == "https://example.invalid/employer/77"
    assert calls == [("employer", "77")]
    # and from now on it is served from the database
    assert (await client.get(f"/api/vacancies/{vacancy}/company")).json()["name"] == "Okko"


async def test_a_repeat_lookup_refreshes_the_one_row(client, vacancy, source):
    await _set_employer_id(vacancy, "77")
    source(fetch_employer=lambda eid: employer(eid, name="Okko"))
    await client.post(f"/api/vacancies/{vacancy}/company")
    source(fetch_employer=lambda eid: employer(eid, name="Okko Renamed"))

    resp = await client.post(f"/api/vacancies/{vacancy}/company")

    assert resp.json()["name"] == "Okko Renamed"
    async with SessionLocal() as session:
        rows = (await session.scalars(
            select(Employer).where(Employer.source == SOURCE_NAME)
        )).all()
    assert [e.name for e in rows] == ["Okko Renamed"]


async def test_an_old_row_gets_its_employer_id_and_nothing_else(client, vacancy, source):
    """The page has changed since it was fetched; the click must not notice."""
    calls = source(fetch=lambda url: page("77", description="EDITED by the employer"),
                   fetch_employer=lambda eid: employer(eid))

    resp = await client.post(f"/api/vacancies/{vacancy}/company")

    assert resp.status_code == 200
    assert calls == [("fetch", "https://example.invalid/c-1"), ("employer", "77")]
    row = await _vacancy(vacancy)
    assert row.employer_external_id == "77"
    assert row.description == "stored text"     # not apply_fetched
    assert row.content_hash == "h" * 64         # so the next match run pays nothing


async def test_a_removed_posting_is_delisted_and_answers_404(client, vacancy, source):
    def gone(url):
        raise VacancyGone("410")
    source(fetch=gone, fetch_employer=lambda eid: pytest.fail("no employer to fetch"))

    resp = await client.post(f"/api/vacancies/{vacancy}/company")

    assert resp.status_code == 404
    assert "removed" in resp.json()["detail"]
    assert (await _vacancy(vacancy)).delisted_at is not None


async def test_an_anonymous_posting_answers_404(client, vacancy, source):
    source(fetch=lambda url: page(None), fetch_employer=lambda eid: pytest.fail("no id"))

    resp = await client.post(f"/api/vacancies/{vacancy}/company")

    assert resp.status_code == 404
    assert "does not name" in resp.json()["detail"]


async def test_a_missing_employer_page_answers_404(client, vacancy, source):
    await _set_employer_id(vacancy, "77")

    def missing(eid):
        raise EmployerGone("404")
    source(fetch_employer=missing)

    assert (await client.post(f"/api/vacancies/{vacancy}/company")).status_code == 404


async def test_a_failing_source_answers_502_with_the_reason(client, vacancy, source):
    await _set_employer_id(vacancy, "77")

    def down(eid):
        raise RuntimeError("hh is down")
    source(fetch_employer=down)

    resp = await client.post(f"/api/vacancies/{vacancy}/company")

    assert resp.status_code == 502
    assert "hh is down" in resp.json()["detail"]


async def test_an_unknown_vacancy_answers_404(client):
    assert (await client.post("/api/vacancies/0/company")).status_code == 404
    assert (await client.get("/api/vacancies/0/company")).json() is None


async def test_a_normal_fetch_stores_the_employer_id(vacancy):
    async with SessionLocal.begin() as session:
        await repository.apply_fetched(session, vacancy, page("77"))
    assert (await _vacancy(vacancy)).employer_external_id == "77"
