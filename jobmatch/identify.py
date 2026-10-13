"""Who is behind a posting: one structured-output call, and a check on its link.

The only module that knows the OpenAI SDK, as `matching/jev.py` is the only one
that knows TypeSafe's. One request through OpenRouter, no tools: the poster's
hh facts and the posting go in, an `Identity` comes out -- the employer, its
homepage and careers page, and whether the poster is a recruiting agency.

`Identity` is the one shape of the answer: its JSON schema is sent as the
response format, it validates what comes back, and its `model_dump()` is what
`employer_identities.answer` stores. Every call is written to `llm_calls` with
the request and the body as received, whether it succeeded or not. The cache
is the pipeline's (`pipeline.identify_employer`); this module always pays.

Design and reasoning: `docs/company-research.md` §2.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import ipaddress
import json
import os
import socket
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlsplit

import anyio
import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import repository

API_KEY_ENV = "OPENROUTER_API_KEY"
BASE_URL = "https://openrouter.ai/api/v1"

MODEL_DEADLINE = 30     # one model request, whole; see _Deadline
MODEL_RETRIES = 1       # so a hang costs at most two deadlines

REACH_DEADLINE = 10     # one reachability check, whole, redirects included
MAX_REDIRECTS = 3       # followed by hand, so each hop's address is checked
# A browser's, because some homepages answer a library's User-Agent with 403
# and would read as "did not answer".
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
}


# --- the input and the answer ---------------------------------------------------

@dataclass(frozen=True, slots=True)
class Poster:
    """What is known about whoever posted the vacancy, before asking."""

    name: str | None        # as the hh employer page gives it
    site_url: str | None    # the link that page publishes; often a careers site
    about: str | None       # hh's city and industries: tells namesakes apart
    posting: str            # the vacancy as Jev reads it (`matching.job_text`)


class Identity(BaseModel):
    """The answer. None means the model did not know, never "there is none".

    Every field is required and nothing extra is allowed, which is what
    OpenAI-style `strict` structured output demands of a schema; pydantic
    already writes `X | None` as `anyOf` with null and `extra="forbid"` as
    `additionalProperties: false`, so the generated schema is sent unchanged
    (`tests/test_identify.py` holds it to that).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    employer: str | None = Field(
        description="The organisation that posted the vacancy, by its common name; "
        "for a division, the parent company"
    )
    homepage: str | None = Field(description="That organisation's main website, a full URL")
    careers_page: str | None = Field(description="Its careers or jobs site, a full URL")
    is_agency: bool | None = Field(
        description="True if the poster is a recruiting agency hiring for a client"
    )
    client: str | None = Field(
        description="An agency's client, only if the posting names it"
    )
    notes: str = Field(description="One or two sentences: how you decided, what is unsure")

    @field_validator("homepage", "careers_page")
    @classmethod
    def _with_scheme(cls, url: str | None) -> str | None:
        """`ertelecom.ru` -> `https://ertelecom.ru`, so the stored answer, the
        reachability check and the UI's link are one URL. A model asked for a
        full URL still answers with a bare domain at times; the raw answer stays
        in `llm_calls.response`. Anything that is not then http(s) -- a
        `javascript://` the UI would run on click -- is stored as unknown, as
        hh's `_web_url` does for its link. The schema is unchanged by this."""
        if url and "://" not in url:
            url = f"https://{url}"
        if url and urlsplit(url).scheme in ("http", "https"):
            return url
        return None


SCHEMA = Identity.model_json_schema()

SYSTEM = """\
You identify the organisation behind a job posting from hh.ru. Use only the
facts given and what you already know about real companies. For anything you
do not know, answer null: a null is useful, a guess is not.

- The link from the hh.ru page is whatever the poster chose to publish. It is
  often a careers or HR site (https://yandex.ru/jobs, https://rabota.vtb.ru)
  rather than the homepage; put each where it belongs, as a full URL.
- A posting from a division ("Банк ВТБ (ПАО), Технологический блок") is the
  parent company's ("ВТБ").
- A recruiting agency says so: "наш клиент", "для нашего клиента", "кадровое
  агентство". Then the employer is the agency, and the client is named only if
  the posting names it. Never infer a client from a description such as
  "крупный банк" or "международная IT-компания"."""


def user_prompt(poster: Poster) -> str:
    return "\n".join([
        f"Name on hh.ru: {poster.name or 'unknown'}",
        f"Link on its hh.ru page: {poster.site_url or 'none'}",
        f"About, from hh.ru: {poster.about or 'unknown'}",
        "",
        "The posting:",
        "",
        poster.posting,
    ])


# OpenRouter's own request fields, sent as the SDK's `extra_body`: the call's
# cost in `usage`, and only providers that honour every parameter -- without
# that, one that ignores `response_format` may answer in free text.
OPENROUTER = {"usage": {"include": True}, "provider": {"require_parameters": True}}


def request_body(poster: Poster, model: str) -> dict[str, Any]:
    """The whole request, exactly as sent. Stored as `llm_calls.request`, and
    what `fingerprint` hashes."""
    # "Exactly as sent" holds only while `_send` passes these fields on and the
    # SDK adds none: `test_the_request_asks_for_strict_json_from_providers_that_
    # honour_it` compares this dict with the body the mocked OpenRouter received.
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt(poster)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "employer_identity", "strict": True, "schema": SCHEMA},
        },
        **OPENROUTER,
    }


def fingerprint(poster: Poster, model: str) -> str:
    """sha256 of the whole request, by the rule in `docs/architecture.md` §4.

    The system prompt, the user prompt (so every `Poster` field), the schema
    with its descriptions and the model: editing any of them makes a different
    request and so a different key, with no version constant to forget.
    """
    blob = json.dumps(
        request_body(poster, model), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode()).hexdigest()


# --- the call ------------------------------------------------------------------

async def ask(
    poster: Poster,
    model: str,
    *,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,   # tests pass a mocked one
) -> tuple[Identity, int]:
    """Ask once, validate, and record the call. Returns the answer and its
    `llm_calls` id; on any failure the row is written, then the error raised.

    `create`, not the SDK's `parse`: the body is read and validated here, so
    when validation fails the body that was paid for is still in hand for the
    trace. The write is shielded: under anyio (FastAPI) a cancelled request --
    a closed browser tab -- re-raises cancellation at every await, which would
    otherwise lose the record of a call that was billed. Hence `BaseException`.
    The shield holds against one cancellation; a second, while the row is being
    written, still interrupts it -- an accepted gap for a single-user tool.
    """
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise ValueError(f"Set {API_KEY_ENV} in .env before identifying employers.")

    request = request_body(poster, model)
    row = {
        "kind": "employer",
        "vacancy_id": vacancy_id,
        "inputs_fingerprint": fingerprint(poster, model),
        "model_requested": model,
        "request": request,
        "started_at": dt.datetime.now(dt.UTC),
    }
    clock = time.monotonic()
    body: dict[str, Any] | None = None
    try:
        body = await _send(request, key, http_client)
        identity = Identity.model_validate_json(_content(body))
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            await _record(row, clock, body, exc)
        raise
    with anyio.CancelScope(shield=True):
        return identity, await _record(row, clock, body, None)


async def _send(
    request: dict[str, Any], key: str, http_client: httpx2.AsyncClient | None
) -> dict[str, Any]:
    """One chat completion; the response body as received."""
    async with AsyncOpenAI(
        api_key=key,
        base_url=BASE_URL,
        max_retries=MODEL_RETRIES,
        http_client=http_client or _Deadline(),
    ) as client:
        raw = await client.chat.completions.with_raw_response.create(
            model=request["model"],
            messages=request["messages"],
            response_format=request["response_format"],
            extra_body=OPENROUTER,
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


async def _record(
    row: dict[str, Any], clock: float, body: dict[str, Any] | None, exc: BaseException | None
) -> int:
    """The ledger row, from whatever the call got as far as.

    An HTTP error carries OpenRouter's error body, which is stored as the
    response when no successful body exists.
    """
    if body is None and isinstance(error_body := getattr(exc, "body", None), dict):
        response = error_body
    else:
        response = body
    usage = (body or {}).get("usage") or {}
    cost = usage.get("cost")
    return await repository.record_call(
        **row,
        response=response,
        status="ok" if exc is None else "error",
        http_status=getattr(exc, "status_code", None),
        error_type=None if exc is None else type(exc).__name__,
        error_message=None if exc is None else str(exc)[:2000],
        model_resolved=(body or {}).get("model"),
        provider=(body or {}).get("provider"),   # the upstream OpenRouter routed to
        response_id=(body or {}).get("id"),
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cost_usd=None if cost is None else Decimal(str(cost)),
        duration_ms=int((time.monotonic() - clock) * 1000),
        finished_at=dt.datetime.now(dt.UTC),
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


# --- does the homepage answer --------------------------------------------------

async def reachable(url: str, *, http_client: httpx2.AsyncClient | None = None) -> bool:
    """Does `url` answer below 400, within `REACH_DEADLINE`, in at most
    `MAX_REDIRECTS` hops, each of them public? Never raises.

    A hallucinated homepage usually does not resolve; this is what keeps it
    from being shown as a link. It does not check that the page is about the
    employer: a Cyrillic name against a Latin site would fail real homepages.
    """
    try:
        async with asyncio.timeout(REACH_DEADLINE):
            async with http_client or httpx2.AsyncClient(
                headers=HEADERS, follow_redirects=False
            ) as client:
                return await _answers(client, url)
    except (TimeoutError, httpx2.HTTPError, ValueError):   # ValueError: a malformed URL
        return False


async def _answers(client: httpx2.AsyncClient, url: str) -> bool:
    for _ in range(MAX_REDIRECTS + 1):
        if not await _public(url):
            return False
        async with client.stream("GET", url) as resp:   # the body is never read
            if resp.is_redirect and (location := resp.headers.get("location")):
                url = urljoin(url, location)
                continue
            return resp.status_code < 400
    return False                                        # too many redirects


async def _public(url: str) -> bool:
    """http(s), and every address the host resolves to is public.

    The URL comes from a model's answer, and this machine answers on localhost
    with CV-derived data (the API on :8000). The check resolves the name
    itself, so a public hostname pointing at 127.0.0.1 is refused too. (The
    HTTP client resolves again on connect; a rebinding DNS server could slip
    between the two, which is beyond what a single-user tool needs to stop.)
    """
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)
