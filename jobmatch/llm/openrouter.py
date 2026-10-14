"""Structured-output questions through OpenRouter; the only importer of openai. `ask` always pays."""
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

# The cost in `usage`, and only providers that honour `response_format`, or one may answer in free text.
OPENROUTER = {"usage": {"include": True}, "provider": {"require_parameters": True}}


@dataclass(frozen=True, slots=True)
class Question[In, Out: BaseModel]:
    """`answer` is sent as the strict schema, so it must be `extra="forbid"` with every field required."""

    kind: str                       # its `llm_calls.kind`
    schema_name: str                # `json_schema.name` in the request; part of the fingerprint
    system: str
    prompt: Callable[[In], str]
    answer: type[Out]


def request[In, Out: BaseModel](q: Question[In, Out], given: In, model: str) -> dict[str, Any]:
    """Exactly as sent: stored as `llm_calls.request`, and what the fingerprint hashes."""
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
    """`create`, not `parse`: a body that fails validation was still paid for, so it is kept."""
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
    """The rest as `extra_body`, so the request is sent exactly as stored (a test compares the two)."""
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
    """A refusal or empty choice is an error like invalid JSON: paid for, nothing to store."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not content:
        raise ValueError("the response holds no answer; see its body in the trace")
    return content


def _usage(body: dict[str, Any]) -> Usage:
    """From the body: the cost is OpenRouter's own field, not the SDK's."""
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
    """Measured 2026-10-07: OpenRouter sends whitespace every 3 s, so a read timeout never fires."""

    async def send(self, request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
        try:
            async with asyncio.timeout(MODEL_DEADLINE):
                return await super().send(request, **kwargs)
        except TimeoutError:
            raise httpx2.ReadTimeout(
                f"no answer within {MODEL_DEADLINE}s", request=request
            ) from None
