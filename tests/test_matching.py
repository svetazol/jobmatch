"""The matcher: the fingerprint, the answer flattening, and the call ledger.

No network — a fake client stands in for Jev. The database is real, because
the point of `llm_calls` is that a row survives a failure, and only a real
transaction can show that.
"""
import pytest
from sqlalchemy import delete, select
from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer

from jobmatch import matching
from jobmatch.db import SessionLocal
from jobmatch.matching import jev
from jobmatch.models import LlmCall, MatchResult, Vacancy

FINGERPRINT = "f" * 64

pytestmark = pytest.mark.anyio
LEGEND = {
    0: {"label": "poor", "description": "Major mismatch."},
    1: {"label": "weak", "description": "Significant gaps."},
    2: {"label": "good", "description": "Solid match."},
    3: {"label": "strong", "description": "Very close."},
    4: {"label": "excellent", "description": "Ideal."},
}


def answers(score=2.4, probabilities=None):
    return {
        "is_qualified": NoulAnswer(noul=0.74),
        "overall_fit": ScoreAnswer(
            score=score,
            legend=LEGEND,
            probabilities=probabilities or {0: 0.0, 1: 0.07, 2: 0.47, 3: 0.43, 4: 0.03},
            confidence=0.54,
        ),
        "top_gap": ChoiceAnswer(
            choice="technical_skills",
            probabilities={"technical_skills": 0.87, "none": 0.13},
            confidence=0.84,
        ),
        "best_angle": ChoiceAnswer(
            choice="ai_llm",
            probabilities={"ai_llm": 0.6, "backend": 0.4},
            confidence=0.72,
        ),
    }


class FakeResponse:
    model = "typesafe/jev-1.13-20260917"

    class usage:  # noqa: N801 — mirrors the SDK's attribute, not a class name
        input_tokens = 9848
        output_tokens = 90

    def __init__(self, **kwargs):
        self.answers = answers(**kwargs)

    @property
    def raw_http_response(self):
        class Raw:
            @staticmethod
            def json():
                return {
                    "usage": {"cost": 0.000413616},
                    "id": "gen-dec-123",
                    "provider": "TypeSafe",
                }

        return Raw()


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls = 0

    async def system_one(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
async def vacancy():
    async with SessionLocal.begin() as session:
        v = Vacancy(
            source="test-matching",
            external_id="1",
            url="https://example.test/vacancy/1",
            title="Python Developer",
            description="Build things.",
            skills=["Python"],
        )
        session.add(v)
        await session.flush()
        vacancy_id = v.id
    yield vacancy_id
    async with SessionLocal.begin() as session:
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id == vacancy_id))
        await session.execute(delete(Vacancy).where(Vacancy.id == vacancy_id))


async def load(vacancy_id):
    async with SessionLocal() as session:
        return await session.get_one(Vacancy, vacancy_id)


async def calls_for(vacancy_id):
    async with SessionLocal() as session:
        return list(
            await session.scalars(select(LlmCall).where(LlmCall.vacancy_id == vacancy_id))
        )


# --- the fingerprint ------------------------------------------------------


def test_fingerprint_covers_every_input_that_should_invalidate():
    base = matching.inputs_fingerprint("cv", "job", matching.QUESTIONS, "jev-1.13")

    assert base == matching.inputs_fingerprint("cv", "job", matching.QUESTIONS, "jev-1.13")
    assert base != matching.inputs_fingerprint("cv edited", "job", matching.QUESTIONS, "jev-1.13")
    assert base != matching.inputs_fingerprint("cv", "job edited", matching.QUESTIONS, "jev-1.13")
    assert base != matching.inputs_fingerprint("cv", "job", matching.QUESTIONS, "jev-1.14")

    reworded = dict(matching.QUESTIONS)
    reworded.pop("top_gap")
    assert base != matching.inputs_fingerprint("cv", "job", reworded, "jev-1.13")


# --- the answer shape -----------------------------------------------------


def test_score_is_normalised_off_the_legend_scale():
    """Jev returns 2.4 on a 0..4 rubric, not 0-1. The column is 0-1."""
    unit, label, confidence = jev._score_to_unit(answers()["overall_fit"])

    assert unit == pytest.approx(0.6)  # 2.4 / 4
    assert label == "good"             # argmax, not round(2.4)
    assert confidence == 0.54


def test_the_label_is_the_most_probable_level_not_the_rounded_score():
    answer = answers(score=2.6, probabilities={0: 0, 1: 0, 2: 0.51, 3: 0.49, 4: 0})[
        "overall_fit"
    ]
    unit, label, _ = jev._score_to_unit(answer)

    assert unit == pytest.approx(0.65)
    assert label == "good"  # round(2.6) would say "strong"


def test_a_single_level_rubric_does_not_divide_by_zero():
    answer = ScoreAnswer(
        score=0.0, legend={0: {"label": "only"}}, probabilities={0: 1.0}, confidence=1.0
    )
    assert jev._score_to_unit(answer) == (0.0, "only", 1.0)


# --- the ledger -----------------------------------------------------------


async def test_a_successful_call_is_flattened_and_costed(vacancy):
    client = FakeClient(response=FakeResponse())

    outcome = await jev.match_vacancy(
        client,
        "cv",
        await load(vacancy),
        "jev-1.13",
        fingerprint=FINGERPRINT,
        cv_hash="a" * 64,
        content_hash="b" * 64,
        job="job text",
    )

    assert outcome.overall_fit_score == pytest.approx(0.6)
    assert outcome.overall_fit_label == "good"
    assert outcome.is_qualified_noul == 0.74
    assert outcome.top_gap == "technical_skills"
    assert outcome.best_angle == "ai_llm"
    assert outcome.best_angle_confidence == 0.72
    assert outcome.answers["overall_fit"]["score"] == 2.4  # the raw value is kept

    (call,) = await calls_for(vacancy)
    assert call.status == "ok"
    assert call.model_requested == "jev-1.13"
    assert call.model_resolved == "typesafe/jev-1.13-20260917"  # differs; worth storing
    assert float(call.cost_usd) == pytest.approx(0.000413616)
    assert str(call.cost_usd) == "0.000413616"  # not rounded away by the column
    assert call.response_id == "gen-dec-123"
    assert call.input_tokens == 9848


async def test_a_failed_call_is_recorded_and_re_raised(vacancy):
    error = RuntimeError("boom")
    error.status = 429
    client = FakeClient(error=error)

    with pytest.raises(RuntimeError):
        await jev.match_vacancy(
            client,
            "cv",
            await load(vacancy),
            "jev-1.13",
            fingerprint=FINGERPRINT,
            cv_hash="a" * 64,
            content_hash="b" * 64,
            job="job text",
        )

    (call,) = await calls_for(vacancy)
    assert call.status == "error"
    assert call.http_status == 429
    assert call.error_type == "RuntimeError"
    assert call.error_message == "boom"
    assert call.cost_usd is None


async def test_the_job_text_leaves_out_what_the_posting_doesnt_have(vacancy):
    v = await load(vacancy)
    text = matching.job_text(v)

    assert "Salary" not in text  # this posting has none; an empty line is noise
    assert "# Python Developer" in text
    assert "- Python" in text


# --- the dry run -----------------------------------------------------------


async def test_preview_returns_the_same_answers_but_records_nothing(vacancy):
    """The whole contract of --dry-run: identical flattening, zero rows."""
    client = FakeClient(response=FakeResponse())

    preview = await jev.preview_vacancy(
        client, "cv", await load(vacancy), "jev-1.13", job="job text"
    )

    assert preview.overall_fit_score == pytest.approx(0.6)
    assert preview.overall_fit_label == "good"
    assert preview.is_qualified_noul == 0.74
    assert preview.top_gap == "technical_skills"
    assert float(preview.cost_usd) == pytest.approx(0.000413616)
    assert preview.answers["overall_fit"]["score"] == 2.4

    assert await calls_for(vacancy) == []          # no llm_calls row
    async with SessionLocal() as session:
        assert (await session.scalars(
            select(MatchResult).where(MatchResult.vacancy_id == vacancy)
        )).all() == []                       # and no match_results row


async def test_preview_and_match_agree_on_every_promoted_field(vacancy):
    """A dry run that disagreed with the real thing would be worse than none."""
    preview = await jev.preview_vacancy(
        FakeClient(response=FakeResponse()), "cv", await load(vacancy), "jev-1.13", job="job"
    )
    outcome = await jev.match_vacancy(
        FakeClient(response=FakeResponse()),
        "cv",
        await load(vacancy),
        "jev-1.13",
        fingerprint=FINGERPRINT,
        cv_hash="a" * 64,
        content_hash="b" * 64,
        job="job",
    )

    for field in (
        "is_qualified_noul",
        "overall_fit_score",
        "overall_fit_label",
        "overall_fit_confidence",
        "top_gap",
        "top_gap_confidence",
        "best_angle",
        "best_angle_confidence",
        "answers",
    ):
        assert getattr(preview, field) == getattr(outcome, field), field
