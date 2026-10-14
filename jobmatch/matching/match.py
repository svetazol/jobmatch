"""One match: Jev's answer, flattened into the plain values `save_match()` writes."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..llm import jev
from ..models import Vacancy
from .questions import QUESTIONS, QUESTIONS_HASH


@dataclass(frozen=True, slots=True)
class Preview:
    """A `MatchOutcome` without a stored call, so a dry run shows what a real one would save."""

    is_qualified_noul: float
    overall_fit_score: float  # normalised 0-1; the raw value is in answers
    overall_fit_label: str
    overall_fit_confidence: float
    top_gap: str
    top_gap_confidence: float
    best_angle: str
    best_angle_confidence: float
    answers: dict[str, Any]
    cost_usd: Decimal | None
    duration_ms: int


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """A paid call that produced a usable answer, flattened for storage."""

    inputs_fingerprint: str
    vacancy_content_hash: str
    cv_hash: str
    questions_hash: str

    is_qualified_noul: float
    overall_fit_score: float  # normalised 0-1; the raw value is in answers
    overall_fit_label: str
    overall_fit_confidence: float
    top_gap: str
    top_gap_confidence: float
    best_angle: str
    best_angle_confidence: float

    answers: dict[str, Any]
    call_id: int  # the already-committed llm_calls row


def state(cv: str, job: str) -> dict[str, str]:
    """Under the names the questions refer to."""
    return {"cv": cv, "job_description": job}


async def preview_vacancy(
    client: jev.Client, cv: str, vacancy: Vacancy, model: str, *, job: str
) -> Preview:
    """Records nothing: a dry run's spend is in `Preview.cost_usd` only."""
    reply = await jev.preview(client, state(cv, job), QUESTIONS, model)
    return _flatten(reply.response, reply.usage.cost_usd, reply.duration_ms)


async def match_vacancy(
    client: jev.Client,
    cv: str,
    vacancy: Vacancy,
    model: str,
    *,
    cv_hash: str,
    content_hash: str,
    job: str,
) -> MatchOutcome:
    """Records the call either way, then raises or returns."""
    reply = await jev.ask(
        client, state(cv, job), QUESTIONS, model, kind="match", vacancy_id=vacancy.id
    )

    # An SDK shape change raises below, but the spend is already recorded.
    preview = _flatten(reply.response, reply.usage.cost_usd, reply.duration_ms)
    assert reply.call_id is not None   # `ask` always records; only `preview` leaves it None
    return MatchOutcome(
        inputs_fingerprint=reply.fingerprint,
        vacancy_content_hash=content_hash,
        cv_hash=cv_hash,
        questions_hash=QUESTIONS_HASH,
        is_qualified_noul=preview.is_qualified_noul,
        overall_fit_score=preview.overall_fit_score,
        overall_fit_label=preview.overall_fit_label,
        overall_fit_confidence=preview.overall_fit_confidence,
        top_gap=preview.top_gap,
        top_gap_confidence=preview.top_gap_confidence,
        best_angle=preview.best_angle,
        best_angle_confidence=preview.best_angle_confidence,
        answers=preview.answers,
        call_id=reply.call_id,
    )


def _score_to_unit(answer: Any) -> tuple[float, str, float]:
    """Jev scores on a 0..levels-1 scale; the label is the likeliest level, not the rounded score."""
    levels = len(answer.legend)
    top = max(answer.probabilities, key=lambda i: answer.probabilities[i])
    entry = answer.legend[top]
    label = entry["label"] if isinstance(entry, dict) else str(entry)
    unit = answer.score / (levels - 1) if levels > 1 else float(answer.score)
    return min(max(unit, 0.0), 1.0), label, answer.confidence


def _flatten(response: Any, cost: Decimal | None, duration_ms: int) -> Preview:
    """The one place that knows the SDK's answer shape."""
    answers = response.answers
    score, label, confidence = _score_to_unit(answers["overall_fit"])
    gap = answers["top_gap"]
    angle = answers["best_angle"]
    return Preview(
        is_qualified_noul=answers["is_qualified"].noul,
        overall_fit_score=score,
        overall_fit_label=label,
        overall_fit_confidence=confidence,
        top_gap=gap.choice,
        top_gap_confidence=gap.confidence,
        best_angle=angle.choice,
        best_angle_confidence=angle.confidence,
        answers={name: a.model_dump(mode="json") for name, a in answers.items()},
        cost_usd=cost,
        duration_ms=duration_ms,
    )

