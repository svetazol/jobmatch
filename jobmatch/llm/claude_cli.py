"""The only runner of `claude -p`, the user's signed-in Claude Code, so no key is needed."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from decimal import Decimal
from typing import Any

from . import Usage, fingerprint, recorded

TIMEOUT_S = 180


class ClaudeError(RuntimeError):
    """`claude` is missing, timed out, or returned an error; the message is shown to the user as is."""


async def ask(model: str, system: str, prompt: str, *, kind: str, vacancy_id: int) -> str:
    """Run `claude -p` once, recorded; the answer's text."""
    exe = shutil.which("claude")
    if exe is None:     # before the ledger: nothing was sent, so nothing was paid for
        raise ClaudeError("the `claude` CLI is not on PATH")
    request = {"model": model, "system": system, "prompt": prompt}
    async with recorded(
        kind=kind, vacancy_id=vacancy_id, fingerprint=fingerprint(request),
        model=model, request=request,
    ) as call:
        result, returncode = await _run(exe, request)
        call.response, call.usage = result, _usage(result)
        if result.get("is_error") or returncode != 0:
            raise ClaudeError(f"`claude` failed: {result.get('result') or result.get('subtype')}")
    return str(result["result"]).strip()


async def _run(exe: str, request: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """In a temp dir to keep the project's CLAUDE.md out; the prompt on stdin, as it can outgrow argv."""
    proc = await asyncio.create_subprocess_exec(
        exe, "-p",
        "--output-format", "json",
        "--model", request["model"],
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", request["system"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tempfile.gettempdir(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(request["prompt"].encode()), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeError(f"`claude` did not answer within {TIMEOUT_S}s") from None
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        detail = (err or out).decode(errors="replace").strip()[-500:]
        raise ClaudeError(f"`claude` exited {proc.returncode}: {detail}") from None
    return result, proc.returncode


def _usage(result: dict[str, Any]) -> Usage:
    """What `claude -p` reports about the call; any of it may be missing."""
    usage = result.get("usage") or {}
    cost = result.get("total_cost_usd")
    return Usage(
        response_id=result.get("session_id"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cost_usd=None if cost is None else Decimal(str(cost)),
    )
