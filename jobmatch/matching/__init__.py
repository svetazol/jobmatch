"""Matching a CV against vacancies through `llm.jev`."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..llm import jev
from ..llm.jev import CONCURRENCY, open_client
from .job import job_text
from .match import MatchOutcome, Preview, match_vacancy, preview_vacancy, state
from .questions import QUESTIONS, QUESTIONS_HASH
from .sanitize import load_text, sanitize_cv

__all__ = [
    "CONCURRENCY",
    "QUESTIONS",
    "QUESTIONS_HASH",
    "MatchOutcome",
    "Preview",
    "content_hash",
    "inputs_fingerprint",
    "job_text",
    "load_cv",
    "match_vacancy",
    "open_client",
    "preview_vacancy",
    "sha256",
]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cv(path: Path | str) -> str:
    """Once per run, not once per vacancy."""
    return sanitize_cv(load_text(Path(path)))


content_hash = sha256


def inputs_fingerprint(cv: str, job: str, questions: dict[str, Any], model: str) -> str:
    """For the cache check: the same hash `jev.ask` records for this request."""
    return jev.fingerprint(state(cv, job), questions, model)
