"""What every model call shares, whichever provider answers it: the key, the fingerprint and the ledger."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anyio

from .. import repository

API_KEY_ENV = "OPENROUTER_API_KEY"   # Jev and the research questions both go through OpenRouter


def api_key(purpose: str) -> str:
    """The key, or a `ValueError` naming what it was for: "Set … in .env {purpose}."."""
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise ValueError(f"Set {API_KEY_ENV} in .env {purpose}.")
    return key


def fingerprint(payload: dict[str, Any]) -> str:
    """sha256 of the whole request (`docs/architecture.md` §4): the idempotency key, no version constant."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Usage:
    """What a provider reports about one call; any of it may be missing from a response."""

    model: str | None = None          # the model that answered, which may differ from the one asked
    provider: str | None = None       # the upstream OpenRouter routed to
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None


@dataclass(slots=True)
class Call:
    """A call being recorded: the caller fills in what it learns; `id` is the row's, after the block."""

    usage: Usage = field(default_factory=Usage)
    response: dict[str, Any] | None = None
    id: int | None = None


@asynccontextmanager
async def recorded(
    *, kind: str, vacancy_id: int, fingerprint: str, model: str,
    request: dict[str, Any] | None = None,
) -> AsyncIterator[Call]:
    """Time the block and write its one `llm_calls` row, ok or error, then re-raise any error.

    The write is shielded: a cancelled request (a closed tab) must not lose the record of a billed call.
    """
    row = {
        "kind": kind,
        "vacancy_id": vacancy_id,
        "inputs_fingerprint": fingerprint,
        "model_requested": model,
        "request": request,
        "started_at": dt.datetime.now(dt.UTC),
    }
    clock = time.monotonic()
    call = Call()
    try:
        yield call
    except BaseException as exc:     # cancellation too: it was paid for all the same
        with anyio.CancelScope(shield=True):
            call.id = await _record(row, clock, call, exc)
        raise
    with anyio.CancelScope(shield=True):
        call.id = await _record(row, clock, call, None)


async def _record(row: dict[str, Any], clock: float, call: Call, exc: BaseException | None) -> int:
    """The ledger row, from whatever the call got as far as."""
    response = call.response
    # An HTTP error carries the provider's error body; kept when nothing better came back.
    if response is None and isinstance(error_body := getattr(exc, "body", None), dict):
        response = error_body
    usage = call.usage
    return await repository.record_call(
        **row,
        response=response,
        status="ok" if exc is None else "error",
        http_status=getattr(exc, "status_code", None),
        error_type=None if exc is None else type(exc).__name__,
        error_message=None if exc is None else str(exc)[:2000],
        model_resolved=usage.model,
        provider=usage.provider,
        response_id=usage.response_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
        duration_ms=int((time.monotonic() - clock) * 1000),
        finished_at=dt.datetime.now(dt.UTC),
    )
