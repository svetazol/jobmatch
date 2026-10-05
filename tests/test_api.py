"""The API over the real database, with rows this file owns and cleans up.

The database is real for the same reason `test_pipeline.py` uses it: what is
under test — the LEFT JOIN that keeps an unmatched vacancy in the list, the
partial index behind "the current match", the filters — is the database's
behaviour, and a mock would only test itself.

Nothing here can reach Jev: no code path in `api/` calls the matcher.
"""
import datetime as dt
from decimal import Decimal

import httpx2
import pytest
from sqlalchemy import delete, func, select

from jobmatch import cover_note
from jobmatch.api.app import app
from jobmatch.api.routers.stats import _cv_hash
from jobmatch.db import SessionLocal
from jobmatch.models import LlmCall, MatchResult, Vacancy

SOURCE_NAME = "test-api"

pytestmark = pytest.mark.anyio

# The market view reports on one CV (`stats_cv_hash`), so rows written under
# any other one are invisible to it. The fixture writes under the configured
# hash — testing the numbers the deployed config actually produces — and falls
# back to a dummy when no CV is pinned.
CV_HASH = _cv_hash() or "v" * 64
OTHER_CV_HASH = "0" * 64


def _cv_scope():
    """The predicate `/api/stats` applies, for tests computing an expectation.

    With a CV pinned the endpoint reads that CV's rows whether or not they are
    current (a re-match under another CV supersedes them and they are still
    that CV's answers); with none pinned it reads the current ones.
    """
    return MatchResult.cv_hash == CV_HASH if _cv_hash() else MatchResult.superseded_at.is_(None)


def _match(vacancy_id, call_id, *, score, label, qualified_noul, angle, gap="none", probs=None):
    return MatchResult(
        vacancy_id=vacancy_id,
        llm_call_id=call_id,
        inputs_fingerprint=f"fp-{vacancy_id}-{score}",
        vacancy_content_hash="c" * 64,
        cv_hash=CV_HASH,
        questions_hash="q" * 64,
        is_qualified_noul=qualified_noul,
        overall_fit_score=score,
        overall_fit_label=label,
        overall_fit_confidence=0.8,
        top_gap=gap,
        top_gap_confidence=0.17,
        best_angle=angle,
        best_angle_confidence=0.9,
        answers={
            "overall_fit": {
                "type": "score",
                "score": score * 4,
                "confidence": 0.8,
                "probabilities": probs or {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.7, "4": 0.2},
                "legend": {"3": {"label": "strong", "description": "Very close match."}},
            },
            "top_gap": {"type": "choice", "choice": gap, "confidence": 0.17,
                        "probabilities": {"none": 0.38, "seniority_level": 0.26,
                                          "domain_experience": 0.25, "technical_skills": 0.11}},
            "best_angle": {"type": "choice", "choice": angle, "confidence": 0.9,
                           "probabilities": {angle: 0.9}},
            "is_qualified": {"type": "noul", "noul": qualified_noul},
        },
    )


@pytest.fixture
async def rows():
    """Three vacancies: two matched, one fetched-but-unmatched."""
    async with SessionLocal.begin() as session:
        made = []
        for i, (title, country, formats) in enumerate([
            ("API Backend Role", "Georgia", ["remote"]),
            ("API Data Role", "Belarus", ["onsite", "hybrid"]),
            ("API Unmatched Role", "Georgia", ["remote"]),
        ]):
            vacancy = Vacancy(
                source=SOURCE_NAME, external_id=f"api-{i}",
                url=f"https://example.invalid/api-{i}", title=title,
                company="Test Co", country=country, work_formats=formats,
                description="text", skills=["Python", "Django"],
                fetched_at=dt.datetime.now(dt.UTC),
                last_seen_at=dt.datetime.now(dt.UTC),
            )
            session.add(vacancy)
            made.append(vacancy)
        await session.flush()

        call = LlmCall(vacancy_id=made[0].id, inputs_fingerprint="f" * 64,
                       model_requested="test", status="ok", cost_usd=Decimal("0.000123"))
        session.add(call)
        await session.flush()

        session.add(_match(made[0].id, call.id, score=0.90, label="excellent",
                           qualified_noul=0.8, angle="backend"))
        session.add(_match(made[1].id, call.id, score=0.20, label="weak",
                           qualified_noul=0.3, angle="data"))
        ids = [v.id for v in made]

    yield ids

    async with SessionLocal.begin() as session:
        await session.execute(delete(MatchResult).where(MatchResult.vacancy_id.in_(ids)))
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(ids)))
        await session.execute(delete(Vacancy).where(Vacancy.id.in_(ids)))


@pytest.fixture
async def client():
    """In-process, on the test's own loop. Starlette's TestClient would run
    the app on a loop of its own, and hand it connections this loop opened."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _find(items, vacancy_id):
    return next((i for i in items if i["id"] == vacancy_id), None)


async def test_unmatched_vacancy_is_listed_with_a_null_match(client, rows):
    """A fetched-but-unmatched row is a first-class state, not a zero."""
    items = (await client.get("/api/vacancies", params={"limit": 200, "source": SOURCE_NAME})).json()["items"]
    row = _find(items, rows[2])
    assert row is not None, "the LEFT JOIN dropped an unmatched vacancy"
    assert row["match"] is None


async def test_ordered_by_score_with_unmatched_last(client, rows):
    items = (await client.get("/api/vacancies", params={"limit": 200, "source": SOURCE_NAME})).json()["items"]
    scores = [i["match"]["overall_fit_score"] for i in items if i["match"]]
    assert scores == sorted(scores, reverse=True)
    positions = [n for n, i in enumerate(items) if i["id"] in rows]
    assert positions == sorted(positions)
    assert items[-1]["match"] is None, "unmatched rows must sort last, not first"


async def test_fit_probabilities_ship_with_every_row(client, rows):
    items = (await client.get("/api/vacancies", params={"limit": 200, "source": SOURCE_NAME})).json()["items"]
    row = _find(items, rows[0])
    assert row["match"]["fit_probabilities"] == {
        "0": 0.0, "1": 0.0, "2": 0.1, "3": 0.7, "4": 0.2
    }


async def test_min_fit_thresholds_on_score_not_label(client, rows):
    ids = {i["id"] for i in (await client.get(
        "/api/vacancies", params={"min_fit": 0.5, "limit": 200, "source": SOURCE_NAME})).json()["items"]}
    assert rows[0] in ids       # .90
    assert rows[1] not in ids   # .20
    assert rows[2] not in ids   # unmatched has no score to clear the bar


@pytest.mark.parametrize("params,expected_index", [
    ({"pitch": ["backend"]}, 0),
    ({"country": ["Belarus"]}, 1),
    ({"work_format": ["onsite"]}, 1),
    ({"qualified": True}, 0),
    ({"q": "Django"}, 0),
])
async def test_filters(client, rows, params, expected_index):
    ids = {i["id"] for i in (await client.get(
        "/api/vacancies", params={**params, "limit": 200, "source": SOURCE_NAME})).json()["items"]}
    assert rows[expected_index] in ids


async def test_detail_carries_the_whole_answers_blob(client, rows):
    body = (await client.get(f"/api/vacancies/{rows[0]}")).json()
    assert sorted(body["answers"]) == ["best_angle", "is_qualified", "overall_fit", "top_gap"]
    # the legend travels with it, so the client never hardcodes questions.py
    assert body["answers"]["overall_fit"]["legend"]["3"]["label"] == "strong"
    assert body["match"]["top_gap_confidence"] == 0.17
    assert body["cost_usd"] is not None


async def test_detail_404(client):
    assert (await client.get("/api/vacancies/99999999")).status_code == 404


async def test_triage_sets_and_clears(client, rows):
    vacancy_id = rows[0]
    assert (await client.patch(f"/api/vacancies/{vacancy_id}/triage", json={"starred": True})).status_code == 204
    assert (await client.get(f"/api/vacancies/{vacancy_id}")).json()["starred_at"] is not None
    # false clears it — this is what Undo sends
    assert (await client.patch(f"/api/vacancies/{vacancy_id}/triage", json={"starred": False})).status_code == 204
    assert (await client.get(f"/api/vacancies/{vacancy_id}")).json()["starred_at"] is None


async def test_hidden_leaves_the_list_but_not_the_database(client, rows):
    vacancy_id = rows[0]
    (await client.patch(f"/api/vacancies/{vacancy_id}/triage", json={"hidden": True}))
    items = (await client.get("/api/vacancies", params={"limit": 200, "source": SOURCE_NAME})).json()["items"]
    assert _find(items, vacancy_id) is None
    assert (await client.get(f"/api/vacancies/{vacancy_id}")).status_code == 200  # no soft delete


async def test_triage_rejects_anything_but_one_field(client, rows):
    assert (await client.patch(f"/api/vacancies/{rows[0]}/triage", json={})).status_code == 422
    assert (await client.patch(f"/api/vacancies/{rows[0]}/triage",
                        json={"seen": True, "hidden": True})).status_code == 422


async def test_triage_404(client):
    assert (await client.patch("/api/vacancies/99999999/triage", json={"seen": True})).status_code == 404


async def test_stats_counts_only_current_matches(client, rows):
    body = (await client.get("/api/stats")).json()
    async with SessionLocal() as session:
        current = await session.scalar(select(func.count(func.distinct(MatchResult.vacancy_id)))
                                 .select_from(MatchResult)
                                 .where(_cv_scope()))
    assert body["total"] == current
    assert sum(body["fit_distribution"].values()) == current
    assert sum(p["n"] for p in body["pitches"]) == current
    for pitch in body["pitches"]:
        assert sum(pitch["distribution"]) == pitch["n"]


async def test_stats_orders_pitches_by_mean_fit_with_other_last(client, rows):
    pitches = (await client.get("/api/stats")).json()["pitches"]
    real = [p for p in pitches if p["pitch"] != "none"]
    assert [p["mean_fit"] for p in real] == sorted(
        (p["mean_fit"] for p in real), reverse=True)
    if any(p["pitch"] == "none" for p in pitches):
        assert pitches[-1]["pitch"] == "none"


async def test_cursor_walks_without_repeating(client, rows):
    """limit 2 against this file's 3 rows, so there is a real second page."""
    first = (await client.get("/api/vacancies",
                       params={"limit": 2, "source": SOURCE_NAME})).json()
    assert first["next_cursor"], "a full page must hand back a cursor"
    second = (await client.get("/api/vacancies",
                        params={"limit": 2, "source": SOURCE_NAME,
                                "cursor": first["next_cursor"]})).json()
    assert {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]} == set()
    assert second["items"], "the second page should hold the remaining row"


async def test_malformed_cursor_is_a_400(client):
    assert (await client.get("/api/vacancies", params={"cursor": "nonsense"})).status_code == 400


async def test_paging_reaches_unmatched_vacancies(client, rows):
    """Walk every page and confirm the unmatched row is actually reached.

    It sorts last (NULLS LAST), so a keyset predicate that compares against a
    NULL score drops it — and drops it silently, which is how this survived
    until the corpus grew past one page.
    """
    seen: list[int] = []
    cursor = None
    for _ in range(10):                       # generous bound, not a fixed count
        params = {"limit": 1, "source": SOURCE_NAME}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/api/vacancies", params=params)).json()
        seen.extend(i["id"] for i in page["items"])
        cursor = page["next_cursor"]
        if not cursor or not page["items"]:
            break

    assert sorted(seen) == sorted(rows), f"paging reached {seen}, expected {rows}"
    assert len(seen) == len(set(seen)), "a row was served twice"


async def test_stats_country_filter_narrows_every_aggregate(client, rows):
    body = (await client.get("/api/stats", params={"country": "Georgia"})).json()
    async with SessionLocal() as session:
        expected = await session.scalar(
            select(func.count())
            .select_from(MatchResult)
            .join(Vacancy, Vacancy.id == MatchResult.vacancy_id)
            .where(_cv_scope(), Vacancy.country == "Georgia")
        )
    assert body["total"] == expected
    assert sum(body["fit_distribution"].values()) == expected
    assert sum(p["n"] for p in body["pitches"]) == expected
    assert expected < (await client.get("/api/stats")).json()["total"]


async def test_stats_country_options_stay_whole_corpus(client, rows):
    """Otherwise the filter eats its own options: pick one country and every
    other one disappears from the list you picked it from."""
    unfiltered = (await client.get("/api/stats")).json()["countries"]
    filtered = (await client.get("/api/stats", params={"country": "Georgia"})).json()["countries"]
    assert filtered == unfiltered
    assert {"Georgia", "Belarus"} <= {c["name"] for c in filtered}


async def test_stats_accepts_several_countries(client, rows):
    one = (await client.get("/api/stats", params={"country": "Georgia"})).json()["total"]
    other = (await client.get("/api/stats", params={"country": "Belarus"})).json()["total"]
    both = (await client.get("/api/stats", params=[("country", "Georgia"), ("country", "Belarus")])).json()
    assert both["total"] == one + other


async def test_stats_country_counts_are_over_current_matches(client, rows):
    """The header prints them next to `total` and the filter selects with
    them, so they count matches — not every posting ever fetched. The one
    gap is postings with no country, which have no bucket to land in."""
    body = (await client.get("/api/stats")).json()
    async with SessionLocal() as session:
        matched_with_country = await session.scalar(
            select(func.count())
            .select_from(MatchResult)
            .join(Vacancy, Vacancy.id == MatchResult.vacancy_id)
            .where(_cv_scope(), Vacancy.country.is_not(None))
        )
    assert sum(c["n"] for c in body["countries"]) == matched_with_country


@pytest.mark.skipif(_cv_hash() is None, reason="no CV pinned; stats spans every CV")
async def test_stats_ignores_matches_from_another_cv(client, rows):
    """Re-matching under a new CV supersedes nothing — the CV is part of the
    fingerprint — so both sets stay current and only the pinned one may count.
    Without this scope every average on the page is taken over two CVs."""
    before = (await client.get("/api/stats")).json()
    async with SessionLocal.begin() as session:
        call_id = await session.scalar(select(LlmCall.id).where(LlmCall.vacancy_id == rows[0]))
        intruder = _match(rows[2], call_id, score=0.99, label="excellent",
                          qualified_noul=0.99, angle="backend")
        intruder.cv_hash = OTHER_CV_HASH
        intruder.inputs_fingerprint = "fp-other-cv"
        session.add(intruder)
    try:
        after = (await client.get("/api/stats")).json()
        assert after["total"] == before["total"]
        assert after["mean_fit"] == before["mean_fit"]
        assert after["countries"] == before["countries"]
    finally:
        async with SessionLocal.begin() as session:
            await session.execute(delete(MatchResult).where(
                MatchResult.inputs_fingerprint == "fp-other-cv"))


async def test_stats_lists_every_cv_the_corpus_has_answers_from(client, rows):
    """Including CVs with no *current* row left: one row per vacancy is
    current, so a finished re-match wipes the previous CV out of that view
    entirely — and its answers are still there to be asked for."""
    body = (await client.get("/api/stats")).json()
    names = {option["name"] for option in body["cvs"]}
    assert body["cv"] is None or body["cv"]["name"] in names
    async with SessionLocal.begin() as session:
        call_id = await session.scalar(select(LlmCall.id).where(LlmCall.vacancy_id == rows[0]))
        older = _match(rows[0], call_id, score=0.10, label="poor",
                       qualified_noul=0.1, angle="data")
        older.cv_hash = OTHER_CV_HASH
        older.inputs_fingerprint = "fp-other-cv"
        older.superseded_at = dt.datetime.now(dt.UTC)   # re-matched away
        session.add(older)
    try:
        listed = (await client.get("/api/stats")).json()["cvs"]
        assert OTHER_CV_HASH in {option["cv_hash"] for option in listed}
        # no file hashes to it, so it is named by its hash and flagged
        option = next(o for o in listed if o["cv_hash"] == OTHER_CV_HASH)
        assert option["on_disk"] is False
        assert option["name"] == OTHER_CV_HASH[:12]

        # and it can be asked for, superseded rows and all
        scoped = (await client.get("/api/stats", params={"cv": option["name"]})).json()
        assert scoped["cv"]["cv_hash"] == OTHER_CV_HASH
        assert scoped["total"] == option["n"]
    finally:
        async with SessionLocal.begin() as session:
            await session.execute(delete(MatchResult).where(
                MatchResult.inputs_fingerprint == "fp-other-cv"))


async def test_stats_picks_a_cv_by_file_name(client, rows):
    """The picker sends the file name; the database only ever knew the hash."""
    body = (await client.get("/api/stats")).json()
    on_disk = [option for option in body["cvs"] if option["on_disk"]]
    if not on_disk:
        pytest.skip("no CV file on disk hashes to a stored match")
    name = on_disk[0]["name"]
    assert name.endswith(".md")
    by_name = (await client.get("/api/stats", params={"cv": name})).json()
    by_hash = (await client.get("/api/stats", params={"cv": on_disk[0]["cv_hash"]})).json()
    assert by_name["cv"]["cv_hash"] == on_disk[0]["cv_hash"]
    assert by_name["total"] == by_hash["total"] == on_disk[0]["n"]


async def test_stats_refuses_an_unknown_cv(client, rows):
    """Silently averaging every CV instead is the one wrong answer that cannot
    be told apart from a right one."""
    response = (await client.get("/api/stats", params={"cv": "nope.md"}))
    assert response.status_code == 404
    assert "nope.md" in response.json()["detail"]


async def test_cover_note_returns_what_claude_wrote(client, rows, monkeypatch):
    async def fake_write(vacancy, match, cv):
        assert match is not None and cv
        return "I fit because."
    monkeypatch.setattr(cover_note, "write", fake_write)
    response = await client.post(f"/api/vacancies/{rows[0]}/cover-note")
    assert response.status_code == 200
    assert response.json() == {"text": "I fit because.", "language": "English"}


async def test_cover_note_passes_claudes_failure_through(client, rows, monkeypatch):
    async def failing_write(vacancy, match, cv):
        raise cover_note.CoverNoteError("`claude` failed: not logged in")
    monkeypatch.setattr(cover_note, "write", failing_write)
    response = await client.post(f"/api/vacancies/{rows[0]}/cover-note")
    assert response.status_code == 502
    assert "not logged in" in response.json()["detail"]


async def test_cover_note_404(client):
    assert (await client.post("/api/vacancies/0/cover-note")).status_code == 404
