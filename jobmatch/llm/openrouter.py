"""Structured-output questions through OpenRouter; the only module that imports openai.

A question's `Out` model is its answer's only shape: sent as the strict `response_format` and
validating the reply, so it must be `extra="forbid"` with every field required. `ask` always pays; caching is the caller's."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel

from . import Usage, api_key, fingerprint, recorded

BASE_URL = "https://openrouter.ai/api/v1"

MODEL_DEADLINE = 30     # one model request, whole; see _Deadline
MODEL_RETRIES = 1       # so a hang costs at most two deadlines

# OpenRouter's own request fields, part of every request: the call's cost in
# `usage`, and only providers that honour every parameter -- without that, one
# that ignores `response_format` may answer in free text.
OPENROUTER = {"usage": {"include": True}, "provider": {"require_parameters": True}}



@dataclass(frozen=True, slots=True)
class Question[In, Out: BaseModel]:
    """One kind of question: its system prompt, how `given` becomes the user prompt, and the answer's schema."""

    kind: str                       # its `llm_calls.kind`
    schema_name: str                # `json_schema.name` in the request; part of the fingerprint
    system: str
    prompt: Callable[[In], str]
    answer: type[Out]


def request[In, Out: BaseModel](q: Question[In, Out], given: In, model: str) -> dict[str, Any]:
    """The whole request, exactly as sent: stored as `llm_calls.request`, and what the fingerprint hashes."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": q.system},
            {"role": "user", "content": q.prompt(given)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": q.schema_name, "strict": True,
                "schema": q.answer.model_json_schema(),
            },
        },
        **OPENROUTER,
    }


async def ask[Out: BaseModel](
    q: Question[Any, Out],
    sent: dict[str, Any],           # `request(q, given, model)`, built once by the caller
    *,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,   # tests pass a mocked one
) -> tuple[Out, dict[str, Any], int]:
    """Send once, validate, record the call; the answer, the body as received, and its row id.

    `create`, not the SDK's `parse`: a body that fails validation was still paid for, so it is kept for the trace.
    """
    key = api_key("before calling OpenRouter")
    async with recorded(
        kind=q.kind, vacancy_id=vacancy_id, fingerprint=fingerprint(sent),
        model=sent["model"], request=sent,
    ) as call:
        body = await _send(sent, key, http_client)
        call.response, call.usage = body, _usage(body)
        parsed = q.answer.model_validate_json(_content(body))
    return parsed, body, call.id


async def _send(
    request: dict[str, Any], key: str, http_client: httpx2.AsyncClient | None
) -> dict[str, Any]:
    """One chat completion; the response body as received.

    Everything but `model` and `messages` goes as the SDK's `extra_body`, so
    the request is sent exactly as stored, whatever fields it holds; the SDK
    adds none. `test_the_request_asks_for_strict_json_from_providers_that_
    honour_it` compares the stored request with the body OpenRouter received.
    """
    model, messages = request["model"], request["messages"]
    rest = {k: v for k, v in request.items() if k not in ("model", "messages")}
    async with AsyncOpenAI(
        api_key=key,
        base_url=BASE_URL,
        max_retries=MODEL_RETRIES,
        http_client=http_client or _Deadline(),
    ) as client:
        raw = await client.chat.completions.with_raw_response.create(
            model=model, messages=messages, extra_body=rest,
        )
    return raw.http_response.json()


def _content(body: dict[str, Any]) -> str:
    """The answer's text. A refusal or an empty choice has none, and is an
    error like invalid JSON: paid for, nothing to store."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not content:
        raise ValueError("the response holds no answer; see its body in the trace")
    return content


def _usage(body: dict[str, Any]) -> Usage:
    """OpenRouter's usage, from the body: the cost is its own field, not the SDK's."""
    usage = body.get("usage") or {}
    cost = usage.get("cost")
    return Usage(
        model=body.get("model"),
        provider=body.get("provider"),
        response_id=body.get("id"),
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cost_usd=None if cost is None else Decimal(str(cost)),
    )


class _Deadline(httpx2.AsyncClient):
    """The OpenAI SDK's client, with a deadline on each whole request.

    Measured 2026-10-07: while the upstream model works, OpenRouter sends
    eleven bytes of whitespace every 3 s, so a per-read timeout never fires and
    a stuck call waits indefinitely. Past the deadline this raises the client's
    own `ReadTimeout`, which the SDK retries (`max_retries`) like any other.
    """

    async def send(self, request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
        try:
            async with asyncio.timeout(MODEL_DEADLINE):
                return await super().send(request, **kwargs)
        except TimeoutError:
            raise httpx2.ReadTimeout(
                f"no answer within {MODEL_DEADLINE}s", request=request
            ) from None
