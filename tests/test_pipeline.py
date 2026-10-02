"""Pipeline behaviour with a fake source, a fake matcher and the real database.

A fake source keeps the network out of it; the database is real because the
guarantees under test — the upsert, the attempt counter, the current-match
index — are the database's, and a mock of them would only test itself.

Nothing here may reach Jev: every test patches `match_vacancy`, and matching
is scoped to this file's own source name, so a real vacancy is never an
argument to a paid call.
"""
import contextlib

import pytest
from sqlalchemy import delete, select

from jobmatch import matching, pipeline, repository
from jobmatch.config import Settings, SourceConfig
from jobmatch.db import SessionLocal
from jobmatch.models import LlmCall, MatchResult, Vacancy
from jobmatch.sources import SOURCES, Listing, Source, VacancyData, VacancyGone

SOURCE_NAME = "test-source"

pytestmark = pytest.mark.anyio


def feed(listings):
    """A `Source.discover` serving these listings, whatever it is asked."""
    async def discover(params, crawl, countries=()):
        for item in listings:
            yield item
    return discover


def fetching(fetch):
    """A `Source.fetch` from a plain function, so tests can stay lambdas."""
    async def fetch_async(url):
        return fetch(url)
    return fetch_async


@pytest.fixture
def settings(tmp_path):
    cv = tmp_path / "cv.md"
    cv.write_text("A CV with enough text to survive sanitising.\n", encoding="utf-8")
    return Settings(
        sources=(
            SourceConfig(name=SOURCE_NAME, search={}, sweeps={"all": ("georgia",)}),
        ),
        fetch_delay=0.0,
        model="test-model",
        cv_path=cv,
    )


@pytest.fixture(autouse=True)
def never_call_jev(monkeypatch):
    """A fake outcome, and a client that would explode if anything used it."""
    outcomes = []

    async def fake_match(client, cv, vacancy, model, *, fingerprint, cv_hash, content_hash, job):
        async with SessionLocal.begin() as session:
            call = LlmCall(
                vacancy_id=vacancy.id,
                inputs_fingerprint=fingerprint,
                model_requested=model,
                status="ok",
            )
            session.add(call)
            await session.flush()
            call_id = call.id
        outcome = matching.MatchOutcome(
            inputs_fingerprint=fingerprint,
            vacancy_content_hash=content_hash,
            cv_hash=cv_hash,
            questions_hash="q" * 64,
            is_qualified_noul=0.7,
            overall_fit_score=0.6,
            overall_fit_label="good",
            overall_fit_confidence=0.5,
            top_gap="technical_skills",
            top_gap_confidence=0.8,
            best_angle="backend",
            best_angle_confidence=0.7,
            answers={"overall_fit": {"score": 2.4}},
            call_id=call_id,
        )
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(pipeline.matching, "match_vacancy", fake_match)
    monkeypatch.setattr(pipeline.matching, "open_client", _null_client)
    return outcomes


@contextlib.asynccontextmanager
async def _null_client():
    yield None


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
            discover=feed(listings),
            fetch=fetching(fetch),
        )
        monkeypatch.setitem(SOURCES, SOURCE_NAME, source)
        return source

    return build


@pytest.fixture(autouse=True)
async def clean():
    async def wipe():
        async with SessionLocal.begin() as session:
            ids = select(Vacancy.id).where(Vacancy.source == SOURCE_NAME)
            await session.execute(delete(MatchResult).where(MatchResult.vacancy_id.in_(ids)))
            await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(ids)))
            await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))

    await wipe()
    yield
    await wipe()


async def stored(external_id: str) -> Vacancy:
    async with SessionLocal() as session:
        return (await session.scalars(
            select(Vacancy).where(
                Vacancy.source == SOURCE_NAME, Vacancy.external_id == external_id
            )
        )).one()


async def test_a_second_run_fetches_nothing(fake_source, settings):
    calls = []

    def fetch(url):
        calls.append(url)
        return data(url.rsplit("/", 1)[-1])

    fake_source([listing("1"), listing("2")], fetch)

    first = await pipeline.run(settings)
    second = await pipeline.run(settings)

    assert (first.discovered, first.fetched, first.skipped) == (2, 2, 0)
    assert (second.discovered, second.fetched, second.skipped) == (2, 0, 2)
    assert len(calls) == 2  # not four
    assert (await stored("1")).description == "A description"


async def test_a_gone_vacancy_is_delisted_and_not_retried(fake_source, settings):
    def fetch(url):
        raise VacancyGone("404")

    fake_source([listing("1")], fetch)

    report = await pipeline.run(settings)
    assert (report.delisted, report.fetched) == (1, 0)
    assert (await stored("1")).delisted_at is not None
    assert (await stored("1")).fetch_attempts == 0  # not a failure to count

    assert (await pipeline.run(settings)).skipped == 1


async def test_a_broken_page_counts_attempts_and_doesnt_kill_the_run(fake_source, settings):
    def fetch(url):
        if url.endswith("1"):
            raise ValueError("layout changed")
        return data("2")

    fake_source([listing("1"), listing("2")], fetch)

    report = await pipeline.run(settings)

    assert report.fetched == 1  # the good one still got through
    assert len(report.failures) == 1
    broken = await stored("1")
    assert broken.fetch_attempts == 1
    assert broken.fetch_error.startswith("ValueError: layout changed")
    assert broken.fetched_at is None

    await pipeline.run(settings)
    await pipeline.run(settings)
    assert (await stored("1")).fetch_attempts == 3

    # capped: the fourth run leaves it alone
    assert (await pipeline.run(settings)).skipped == 2
    assert (await stored("1")).fetch_attempts == 3


async def test_discovery_alone_never_touches_fetched_columns(fake_source, settings):
    fake_source([listing("1")], lambda url: data("1"))
    await pipeline.run(settings)

    async with SessionLocal.begin() as session:
        await repository.upsert_listing(session, SOURCE_NAME, listing("1"))

    after = await stored("1")
    assert after.description == "A description"  # discovery didn't blank it
    assert after.fetched_at is not None


async def test_a_source_that_is_down_costs_a_warning_not_the_run(fake_source, settings):
    async def explode(params, crawl, countries=()):
        raise ConnectionError("feed is down")
        yield

    source = Source(
        name=SOURCE_NAME, hosts=("example.test",), discover=explode,
        fetch=fetching(lambda u: data("1")),
    )
    SOURCES[SOURCE_NAME] = source
    try:
        report = await pipeline.run(settings)
    finally:
        del SOURCES[SOURCE_NAME]

    assert report.discovered == 0
    assert len(report.failures) == 1


# --- the matching phase ---------------------------------------------------


async def current_match(external_id: str) -> MatchResult | None:
    async with SessionLocal() as session:
        return (await session.scalars(
            select(MatchResult)
            .join(Vacancy)
            .where(
                Vacancy.source == SOURCE_NAME,
                Vacancy.external_id == external_id,
                MatchResult.superseded_at.is_(None),
            )
        )).one_or_none()


async def all_matches(external_id: str) -> list[MatchResult]:
    async with SessionLocal() as session:
        return list(
            await session.scalars(
                select(MatchResult)
                .join(Vacancy)
                .where(Vacancy.source == SOURCE_NAME, Vacancy.external_id == external_id)
                .order_by(MatchResult.id)
            )
        )


async def test_a_fetched_vacancy_gets_a_match_and_a_re_run_pays_nothing(fake_source, settings):
    fake_source([listing("1")], lambda url: data("1"))

    first = await pipeline.run(settings)
    second = await pipeline.run(settings)

    assert (first.matched, first.already_matched) == (1, 0)
    assert (second.matched, second.already_matched) == (0, 1)
    assert (await current_match("1")).overall_fit_label == "good"
    assert len(await all_matches("1")) == 1


async def test_editing_the_cv_re_matches_and_reverting_revives_for_free(fake_source, settings):
    fake_source([listing("1")], lambda url: data("1"))
    await pipeline.run(settings)
    original = settings.cv_path.read_text(encoding="utf-8")
    first_id = (await current_match("1")).id

    settings.cv_path.write_text(original + "\nNow I know Kubernetes.\n", encoding="utf-8")
    edited = await pipeline.run(settings)

    assert edited.matched == 1                    # the edit invalidated it
    assert len(await all_matches("1")) == 2
    assert (await current_match("1")).id != first_id

    settings.cv_path.write_text(original, encoding="utf-8")
    reverted = await pipeline.run(settings)

    assert reverted.matched == 0                  # found, not re-paid
    assert reverted.already_matched == 1
    assert len(await all_matches("1")) == 2             # nothing new was written
    assert (await current_match("1")).id == first_id      # the old answer came back


async def test_a_vacancy_outside_todays_feed_is_still_matched(fake_source, settings):
    """The feed is a rolling window; the corpus outgrows it. Matching walks
    the table, so yesterday's vacancy is not stranded without an answer."""
    fake_source([listing("1"), listing("2")], lambda url: data(url.rsplit("/", 1)[-1]))
    await pipeline.run(settings)

    # tomorrow the feed has moved on and only shows a third vacancy
    fake_source([listing("3")], lambda url: data("3"))
    settings.cv_path.write_text("A completely different CV.\n", encoding="utf-8")
    report = await pipeline.run(settings)

    assert report.matched == 3  # not just the one in the feed
    assert await current_match("1") is not None


async def test_a_failed_match_doesnt_kill_the_run(fake_source, settings, monkeypatch):
    fake_source([listing("1"), listing("2")], lambda url: data(url.rsplit("/", 1)[-1]))
    good = pipeline.matching.match_vacancy

    async def sometimes(client, cv, vacancy, model, **kwargs):
        if vacancy.external_id == "1":
            raise RuntimeError("Jev said no")
        return await good(client, cv, vacancy, model, **kwargs)

    monkeypatch.setattr(pipeline.matching, "match_vacancy", sometimes)
    report = await pipeline.run(settings)

    assert report.matched == 1
    assert len(report.failures) == 1
    assert await current_match("1") is None
    assert await current_match("2") is not None


async def test_an_escaped_error_costs_its_vacancy_not_the_calls_in_flight(
    fake_source, settings, monkeypatch
):
    """Matching is concurrent, so a failure outside the matcher's own
    try/except -- the loser of two concurrent runs gets an IntegrityError from
    `save_match` -- must not cancel the paid calls running beside it."""
    fake_source([listing(str(n)) for n in range(5)], lambda url: data(url.rsplit("/", 1)[-1]))
    good = repository.save_match

    async def save(session, vacancy_id, outcome):
        if vacancy_id == broken_id:
            raise RuntimeError("lost the race")
        return await good(session, vacancy_id, outcome)

    await pipeline.run(settings, match=False)
    broken_id = (await stored("1")).id
    monkeypatch.setattr(pipeline.repository, "save_match", save)

    report = await pipeline.match_all(settings)

    assert report.matched == 4                    # the other four all landed
    assert [url for url, _ in report.failures] == [f"match:{broken_id}"]
    assert await current_match("1") is None


async def test_limit_caps_what_a_run_spends(fake_source, settings):
    fake_source([listing(str(n)) for n in range(5)], lambda url: data(url.rsplit("/", 1)[-1]))

    report = await pipeline.run(settings, limit=2)

    assert report.matched == 2
    assert report.fetched == 5  # fetching is free and still finishes


async def test_no_match_crawls_and_fetches_but_never_pays(settings, monkeypatch):
    """`run(match=False)` must not reach the matcher at all.

    Not merely "matches nothing": opening the client is itself a failure here,
    because it requires an API key and the whole point of the flag is to run
    the free phases on a machine that has none.
    """
    def explode(*args, **kwargs):
        raise AssertionError("the paid phase ran despite match=False")

    monkeypatch.setattr(pipeline.matching, "open_client", explode)
    monkeypatch.setattr(pipeline, "match_all", explode)

    SOURCES[SOURCE_NAME] = Source(
        name=SOURCE_NAME,
        hosts=("example.test",),
        discover=feed([listing("1"), listing("2")]),
        fetch=fetching(lambda url: data(url.rsplit("/", 1)[-1])),
    )
    try:
        report = await pipeline.run(settings, match=False)
    finally:
        del SOURCES[SOURCE_NAME]

    assert report.discovered > 0
    assert report.matched == 0
    async with SessionLocal() as session:
        stored = (await session.scalars(
            select(Vacancy).where(Vacancy.source == SOURCE_NAME)
        )).all()
        assert stored, "nothing was crawled"
        assert all(v.fetched_at is not None for v in stored), "fetch phase was skipped too"


async def test_fetch_all_drains_the_queue_without_crawling(settings, monkeypatch):
    """The free-phase twin of `match_all`: no discovery, no spending.

    Discovery must not run — the point of the command is to finish work that
    discovery already did, on a corpus that may be far larger than today's
    search results.
    """
    def no_crawling(*args, **kwargs):
        raise AssertionError("fetch_all must not discover")

    SOURCES[SOURCE_NAME] = Source(
        name=SOURCE_NAME,
        hosts=("example.test",),
        discover=no_crawling,
        fetch=fetching(lambda url: data(url.rsplit("/", 1)[-1])),
    )
    try:
        # two listings stored but never fetched — exactly the queue state a
        # throttled run leaves behind
        async with SessionLocal.begin() as session:
            for n in ("901", "902"):
                await repository.upsert_listing(session, SOURCE_NAME, listing(n))

        report = await pipeline.fetch_all(settings)

        assert report.fetched == 2
        async with SessionLocal() as session:
            assert await repository.fetch_queue(session, [SOURCE_NAME]) == []
    finally:
        del SOURCES[SOURCE_NAME]


async def test_fetch_all_is_a_no_op_on_an_empty_queue(settings):
    report = await pipeline.fetch_all(settings)
    assert report.fetched == 0
    assert report.failures == []


async def test_discover_stores_listings_and_fetches_nothing(settings, monkeypatch):
    """Phase 1 alone leaves rows in the fetch queue and spends nothing."""
    def explode(*a, **k):
        raise AssertionError("discover must not fetch")

    source = Source(
        name=SOURCE_NAME,
        hosts=("example.test",),
        discover=feed([listing("1"), listing("2")]),
        fetch=explode,
    )
    monkeypatch.setitem(SOURCES, SOURCE_NAME, source)

    report = await pipeline.discover_all(settings)
    assert (report.discovered, report.added, report.fetched) == (2, 2, 0)
    async with SessionLocal() as session:
        rows = list(await session.scalars(select(Vacancy).where(Vacancy.source == SOURCE_NAME)))
        queued = await repository.fetch_queue(session, [SOURCE_NAME])
    assert len(rows) == 2
    assert all(v.fetched_at is None and v.description is None for v in rows)
    assert len(queued) == 2        # both are waiting for `fetch --pending`


async def test_a_second_discover_adds_nothing_new(settings, monkeypatch):
    """`added` is what tells a config change apart from a re-crawl."""
    source = Source(
        name=SOURCE_NAME,
        hosts=("example.test",),
        discover=feed([listing("1")]),
        fetch=fetching(lambda url: None),
    )
    monkeypatch.setitem(SOURCES, SOURCE_NAME, source)

    assert (await pipeline.discover_all(settings)).added == 1
    again = await pipeline.discover_all(settings)
    assert (again.discovered, again.added) == (1, 0)


async def test_discover_limit_stops_the_walk_rather_than_trimming(settings, monkeypatch):
    """The point of --limit: a config change costs a page, not a sweep. If it
    trimmed the result instead, the crawl would already have been paid for."""
    yielded = []

    async def endless(params, crawl, countries=()):
        for n in range(1, 500):
            yielded.append(n)
            yield listing(str(n))

    monkeypatch.setitem(
        SOURCES, SOURCE_NAME,
        Source(name=SOURCE_NAME, hosts=("example.test",), discover=endless,
               fetch=fetching(lambda url: None)),
    )
    report = await pipeline.discover_all(settings, limit=3)
    assert report.discovered == 3
    assert len(yielded) == 3        # the generator was abandoned, not drained
