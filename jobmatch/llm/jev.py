"""Jev (TypeSafe AI's System One) through OpenRouter; the only module that calls typesafe_sdk."""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient

from .. import llm

BASE_URL = "https://openrouter.ai/api"  # the SDK appends /v1/systemone itself
# The SDK retries a 429 itself, so too high costs latency rather than answers.
CONCURRENCY = 4

Client = AsyncTypeSafeClient   # what `open_client` yields, for callers that do not import the SDK


@dataclass(frozen=True, slots=True)
class JevAnswer:
    """What Jev answered and what it cost; `call_id` is None for a preview, which records nothing."""

    response: Any                   # the SDK's response; its `answers` are what matching flattens
    usage: llm.Usage
    duration_ms: int
    fingerprint: str                # the hash of exactly what was sent
    call_id: int | None


@asynccontextmanager
async def open_client(api_key: str | None = None) -> AsyncIterator[AsyncTypeSafeClient]:
    """One per run: it holds an HTTP session."""
    key = api_key or llm.api_key("before matching")
    async with AsyncTypeSafeClient(api_key=key, base_url=BASE_URL) as client:
        yield client


def canonical(questions: dict) -> str:
    """The exact serialisation that goes into a fingerprint."""
    payload = {k: q.model_dump(mode="json") for k, q in sorted(questions.items())}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(state: dict[str, str], questions: dict[str, Any], model: str) -> str:
    """The idempotency key: state, questions and model, nothing else."""
    return llm.fingerprint({"state": state, "questions": json.loads(canonical(questions)), "model": model})


async def ask(
    client: AsyncTypeSafeClient,
    state: dict[str, str],
    questions: dict[str, Any],
    model: str,
    *,
    kind: str,
    vacancy_id: int,
) -> JevAnswer:
    """Its `llm_calls` row is written, ok or error, before this returns or raises."""
    key = fingerprint(state, questions, model)
    async with llm.recorded(kind=kind, vacancy_id=vacancy_id, fingerprint=key, model=model) as call:
        response, call.usage, duration_ms = await _send(client, state, questions, model)
    return JevAnswer(response, call.usage, duration_ms, key, call.id)


async def preview(
    client: AsyncTypeSafeClient, state: dict[str, str], questions: dict[str, Any], model: str
) -> JevAnswer:
    """The same call with no ledger row: a dry run's spend is in its answer only."""
    response, usage, duration_ms = await _send(client, state, questions, model)
    return JevAnswer(response, usage, duration_ms, fingerprint(state, questions, model), None)


async def _send(
    client: AsyncTypeSafeClient, state: dict[str, str], questions: dict[str, Any], model: str
) -> tuple[Any, llm.Usage, int]:
    clock = time.monotonic()
    response = await client.system_one(state=state, questions=questions, model=model)
    duration_ms = int((time.monotonic() - clock) * 1000)
    meta = _meta(response)
    cost = meta.get("cost")
    usage = llm.Usage(
        model=response.model,
        provider=meta.get("provider"),
        response_id=meta.get("response_id"),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=None if cost is None else Decimal(str(cost)),
    )
    return response, usage, duration_ms


def _meta(response: Any) -> dict[str, Any]:
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
