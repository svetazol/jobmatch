"""The LLM concern: the CV, the questions, the call, and the hash that stops
us paying for the same answer twice.

Knows nothing about sources, and nothing outside ``jev.py`` knows the SDK.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .jev import MatchOutcome, Preview, job_text, match_vacancy, open_client, preview_vacancy
from .questions import QUESTIONS, QUESTIONS_HASH, canonical
from .sanitize import load_text, sanitize_cv

__all__ = [
    "MatchOutcome",
    "Preview",
    "QUESTIONS",
    "QUESTIONS_HASH",
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
    """Read and sanitize once per run — not once per vacancy."""
    return sanitize_cv(load_text(Path(path)))


content_hash = sha256  # named for what it means at the call site


def inputs_fingerprint(cv: str, job: str, questions: dict[str, Any], model: str) -> str:
    """sha256 of the exact request. **The idempotency key.**

    Everything that should invalidate a stored answer flows through this one
    hash, because the hash covers the whole request: a reworded question, a
    rewritten CV, a new redaction rule in sanitize.py (the CV is hashed
    *after* sanitising), an edited posting, or a different pinned model. There
    is no PROMPT_VERSION to forget to bump.
    """
    payload = {
        "state": {"cv": cv, "job_description": job},
        "questions": json.loads(canonical(questions)),
        "model": model,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(blob)
