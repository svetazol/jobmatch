"""Talking to Jev (TypeSafe AI's System One model) via OpenRouter.

The only module that knows the SDK's answer shape. Everything downstream sees
a ``MatchOutcome`` of plain values, which is what lets ``save_match()`` stay a
dumb column write.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from typesafe_sdk import TypeSafeAPIError, TypeSafeClient

from ..db import SessionLocal
from ..models import LlmCall, Vacancy
from .questions import QUESTIONS, QUESTIONS_HASH

log = logging.getLogger(__name__)

API_KEY_ENV = "OPENROUTER_API_KEY"
BASE_URL = "https://openrouter.ai/api"  # the SDK appends /v1/systemone itself


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """A paid call that produced a usable answer, flattened for storage."""

    inputs_fingerprint: str
    vacancy_content_hash: str
    cv_hash: str
    questions_hash: str

    is_qualified_noul: float
    overall_fit_score: float  # normalised 0–1; the raw scale value is in answers
    overall_fit_label: str
    overall_fit_confidence: float
    top_gap: str
    top_gap_confidence: float

    answers: dict[str, Any]
    call_id: int  # the already-committed llm_calls row


@contextmanager
def open_client(api_key: str | None = None):
    """One client per run — it holds an HTTP session."""
    key = api_key or os.environ.get(API_KEY_ENV)
    if not key:
        raise ValueError(f"Set {API_KEY_ENV} in .env before matching.")
    with TypeSafeClient(api_key=key, base_url=BASE_URL) as client:
        yield client


def job_text(vacancy: Vacancy) -> str:
    """Render a vacancy for the model. Goes to Jev, never to disk.

    Salary and skills are genuinely absent on a fair share of postings, so
    every line is conditional — an empty "Skills:" line is noise the model
    would have to interpret.
    """
    lines = [f"# {vacancy.title}"]
    for label, value in (
        ("Company", vacancy.company),
        ("Salary", vacancy.salary_raw),
        ("Experience", vacancy.experience_raw),
    ):
        if value:
            lines.append(f"{label}: {value}")
    lines += ["", vacancy.description or ""]
    if vacancy.skills:
        lines += ["", "## Key skills", ""] + [f"- {s}" for s in vacancy.skills]
    return "\n".join(lines).strip()


def _score_to_unit(answer: Any) -> tuple[float, str, float]:
    """(normalised score, label, confidence) from a Score answer.

    Jev returns the expected position on the legend's own index scale — 2.4 on
    a 0..4 rubric, *not* 0–1. Normalising here keeps the sort key and any
    min_fit threshold meaningful when a question set has a different number of
    levels; the raw value stays in ``answers``. The label is the most probable
    single level, not the rounded expectation: with 0.47 on "good" and 0.43 on
    "strong", rounding 2.4 would agree by luck, and 2.6 would not.
    """
    levels = len(answer.legend)
    top = max(answer.probabilities, key=lambda i: answer.probabilities[i])
    entry = answer.legend[top]
    label = entry["label"] if isinstance(entry, dict) else str(entry)
    unit = answer.score / (levels - 1) if levels > 1 else float(answer.score)
    return min(max(unit, 0.0), 1.0), label, answer.confidence


def _usage(response: Any) -> dict[str, Any]:
    """cost, id and provider are in the JSON but not in the SDK's model."""
    try:
        body = response.raw_http_response.json()
    except Exception:  # noqa: BLE001 — metadata is never worth failing a call over
        return {}
    usage = body.get("usage") or {}
    return {
        "cost": usage.get("cost"),
        "response_id": body.get("id"),
        "provider": body.get("provider"),
    }


def _record_call(**values: Any) -> int:
    """Write the ledger row in its *own* session, so it survives the caller's
    rollback. A log that disappears when things go wrong is worse than none."""
    with SessionLocal.begin() as session:
        call = LlmCall(**values)
        session.add(call)
        session.flush()
        return call.id


def match_vacancy(
    client: TypeSafeClient,
    cv: str,
    vacancy: Vacancy,
    model: str,
    *,
    fingerprint: str,
    cv_hash: str,
    content_hash: str,
    job: str,
) -> MatchOutcome:
    """Ask Jev about one vacancy. Records the call either way, then raises or
    returns."""
    started = dt.datetime.now(dt.UTC)
    clock = time.monotonic()
    common = {
        "vacancy_id": vacancy.id,
        "inputs_fingerprint": fingerprint,
        "model_requested": model,
        "started_at": started,
    }
    try:
        response = client.system_one(
            state={"cv": cv, "job_description": job}, questions=QUESTIONS, model=model
        )
    except Exception as exc:
        _record_call(
            **common,
            status="error",
            http_status=getattr(exc, "status", None),
            error_type=type(exc).__name__,
            error_message=str(exc)[:2000],
            duration_ms=int((time.monotonic() - clock) * 1000),
            finished_at=dt.datetime.now(dt.UTC),
        )
        raise

    duration_ms = int((time.monotonic() - clock) * 1000)
    meta = _usage(response)
    call_id = _record_call(
        **common,
        status="ok",
        model_resolved=response.model,
        provider=meta.get("provider"),
        response_id=meta.get("response_id"),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=Decimal(str(meta["cost"])) if meta.get("cost") is not None else None,
        duration_ms=duration_ms,
        finished_at=dt.datetime.now(dt.UTC),
    )

    # Anything below here can raise on an SDK shape change; the call is already
    # recorded, so the money spent is visible even when the answer is not.
    answers = response.answers
    score, label, confidence = _score_to_unit(answers["overall_fit"])
    gap = answers["top_gap"]
    return MatchOutcome(
        inputs_fingerprint=fingerprint,
        vacancy_content_hash=content_hash,
        cv_hash=cv_hash,
        questions_hash=QUESTIONS_HASH,
        is_qualified_noul=answers["is_qualified"].noul,
        overall_fit_score=score,
        overall_fit_label=label,
        overall_fit_confidence=confidence,
        top_gap=gap.choice,
        top_gap_confidence=gap.confidence,
        answers={name: a.model_dump(mode="json") for name, a in answers.items()},
        call_id=call_id,
    )
