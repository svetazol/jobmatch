"""Pipeline behaviour with a fake source and the real database.

A fake source keeps the network out of it; the database is real because the
guarantees under test — the upsert, the attempt counter — are the database's,
and a mock of them would only test itself. Rows use their own source name and
are cleaned up around each test.
"""
import pytest
from sqlalchemy import delete, select

from jobmatch import pipeline, repository
from jobmatch.config import Settings, SourceConfig
from jobmatch.db import SessionLocal
from jobmatch.models import Vacancy
from jobmatch.sources import SOURCES, Listing, Source, VacancyData, VacancyGone

SOURCE_NAME = "test-source"
SETTINGS = Settings(
    sources=(SourceConfig(name=SOURCE_NAME, params={}),),
    fetch_delay=0.0,
    max_fetch_attempts=3,
)


def listing(n: str) -> Listing:
    return Listing(external_id=n, url=f"https://example.test/vacancy/{n}", title=f"Job {n}")


def data(n: str) -> VacancyData:
    return VacancyData(
        external_id=n,
        url=f"https://example.test/vacancy/{n}",
        title=f"Job {n}",
        description="A description",
        skills=["Python"],
    )


@pytest.fixture
def fake_source(monkeypatch):
    """A source whose fetch does whatever the test tells it to."""

    def build(listings, fetch):
        source = Source(
            name=SOURCE_NAME,
            hosts=("example.test",),
            discover=lambda params: list(listings),
            fetch=fetch,
        )
        monkeypatch.setitem(SOURCES, SOURCE_NAME, source)
        return source

    return build


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with SessionLocal.begin() as session:
            session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))

    wipe()
    yield
    wipe()


def stored(external_id: str) -> Vacancy:
    with SessionLocal() as session:
        return session.scalars(
            select(Vacancy).where(
                Vacancy.source == SOURCE_NAME, Vacancy.external_id == external_id
            )
        ).one()


def test_a_second_run_fetches_nothing(fake_source):
    calls = []

    def fetch(url):
        calls.append(url)
        return data(url.rsplit("/", 1)[-1])

    fake_source([listing("1"), listing("2")], fetch)

    first = pipeline.run(SETTINGS)
    second = pipeline.run(SETTINGS)

    assert (first.discovered, first.fetched, first.skipped) == (2, 2, 0)
    assert (second.discovered, second.fetched, second.skipped) == (2, 0, 2)
    assert len(calls) == 2  # not four
    assert stored("1").description == "A description"


def test_a_gone_vacancy_is_delisted_and_not_retried(fake_source):
    def fetch(url):
        raise VacancyGone("404")

    fake_source([listing("1")], fetch)

    report = pipeline.run(SETTINGS)
    assert (report.delisted, report.fetched) == (1, 0)
    assert stored("1").delisted_at is not None
    assert stored("1").fetch_attempts == 0  # not a failure to count

    assert pipeline.run(SETTINGS).skipped == 1


def test_a_broken_page_counts_attempts_and_doesnt_kill_the_run(fake_source):
    def fetch(url):
        if url.endswith("1"):
            raise ValueError("layout changed")
        return data("2")

    fake_source([listing("1"), listing("2")], fetch)

    report = pipeline.run(SETTINGS)

    assert report.fetched == 1  # the good one still got through
    assert len(report.failures) == 1
    broken = stored("1")
    assert broken.fetch_attempts == 1
    assert broken.fetch_error.startswith("ValueError: layout changed")
    assert broken.fetched_at is None

    pipeline.run(SETTINGS)
    pipeline.run(SETTINGS)
    assert stored("1").fetch_attempts == 3

    # capped: the fourth run leaves it alone
    assert pipeline.run(SETTINGS).skipped == 2
    assert stored("1").fetch_attempts == 3


def test_discovery_alone_never_touches_fetched_columns(fake_source):
    fake_source([listing("1")], lambda url: data("1"))
    pipeline.run(SETTINGS)

    with SessionLocal.begin() as session:
        repository.upsert_listing(session, SOURCE_NAME, listing("1"))

    after = stored("1")
    assert after.description == "A description"  # discovery didn't blank it
    assert after.fetched_at is not None


def test_a_source_that_is_down_costs_a_warning_not_the_run(fake_source):
    def explode(params):
        raise ConnectionError("feed is down")

    source = Source(
        name=SOURCE_NAME, hosts=("example.test",), discover=explode, fetch=lambda u: data("1")
    )
    SOURCES[SOURCE_NAME] = source
    try:
        report = pipeline.run(SETTINGS)
    finally:
        del SOURCES[SOURCE_NAME]

    assert report.discovered == 0
    assert len(report.failures) == 1
