"""The CLI's named reads (`vacancies_by_ids`, `current_matches`, `count_current_matches`) on the real database."""
import datetime as dt

import pytest
from sqlalchemy import delete

from jobmatch import repository
from jobmatch.db import SessionLocal
from jobmatch.models import LlmCall, MatchResult, Vacancy

SOURCE_NAME = "test-repository"
CV = "r" * 64          # no real CV hashes to this, so the count sees only these rows

pytestmark = pytest.mark.anyio


def _match(vacancy_id: int, call_id: int, fingerprint: str, *, cv: str = CV, superseded: bool = False):
    return MatchResult(
        vacancy_id=vacancy_id, llm_call_id=call_id, inputs_fingerprint=fingerprint,
        vacancy_content_hash="c" * 64, cv_hash=cv, questions_hash="q" * 64,
        is_qualified_noul=0.5, overall_fit_score=0.5, overall_fit_label="good",
        overall_fit_confidence=0.5, top_gap="none", top_gap_confidence=0.5,
        best_angle="backend", best_angle_confidence=0.5, answers={},
        superseded_at=dt.datetime.now(dt.UTC) if superseded else None,
    )


@pytest.fixture
async def rows():
    """Three vacancies: one with a current and a superseded answer, one only superseded, one unmatched."""
    async with SessionLocal.begin() as session:
        made = [Vacancy(source=SOURCE_NAME, external_id=str(i), url=f"https://example.test/{i}")
                for i in range(3)]
        session.add_all(made)
        await session.flush()
        call = LlmCall(kind="match", vacancy_id=made[0].id, inputs_fingerprint="f" * 64,
                       model_requested="jev-1.13", status="ok", started_at=dt.datetime.now(dt.UTC))
        session.add(call)
        await session.flush()
        session.add_all([
            _match(made[0].id, call.id, "old-0", superseded=True),
            _match(made[0].id, call.id, "new-0"),
            _match(made[1].id, call.id, "old-1", superseded=True),
        ])
        ids = [v.id for v in made]
    yield ids
    async with SessionLocal.begin() as session:
        await session.execute(delete(MatchResult).where(MatchResult.vacancy_id.in_(ids)))
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(ids)))
        await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))


async def test_vacancies_by_ids_returns_exactly_those_rows(rows):
    async with SessionLocal() as session:
        found = await repository.vacancies_by_ids(session, rows[:2])
    assert sorted(v.id for v in found) == sorted(rows[:2])


async def test_current_matches_holds_only_current_answers(rows):
    async with SessionLocal() as session:
        current = await repository.current_matches(session, rows)
    assert list(current) == [rows[0]]
    assert current[rows[0]].inputs_fingerprint == "new-0"


async def test_count_current_matches_counts_one_cvs_current_answers(rows):
    async with SessionLocal() as session:
        assert await repository.count_current_matches(session, CV) == 1
        assert await repository.count_current_matches(session, "s" * 64) == 0
