"""What every model call shares, whichever provider answers it: the API key,
the fingerprint that stops us paying twice for the same request, and the
usage a call reports for the ledger.

Plain functions, no client: the providers share no call shape
(docs/decisions.md), only these.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

API_KEY_ENV = "OPENROUTER_API_KEY"   # Jev and the research questions both go through OpenRouter


def api_key(purpose: str) -> str:
    """The OpenRouter key, or a `ValueError` that says what it was needed for:
    `api_key("before matching")` -> "Set OPENROUTER_API_KEY in .env before matching."."""
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise ValueError(f"Set {API_KEY_ENV} in .env {purpose}.")
    return key


def fingerprint(payload: dict[str, Any]) -> str:
    """sha256 of a request, by the rule in `docs/architecture.md` §4. **The
    idempotency key.**

    The payload is the whole request -- prompts, schema, model -- so editing
    any part of it makes a different key, with no version constant to forget.
    Sorted and compact, so the same request always hashes the same: the step-0
    pins in the tests hold this serialisation still.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Usage:
    """What a provider reports about one call, for its `llm_calls` row. Any
    field may be missing from a response, so each defaults to unknown."""

    model: str | None = None          # the model that answered, which may differ from the one asked
    provider: str | None = None       # the upstream OpenRouter routed to
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
