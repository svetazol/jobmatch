import json

import anyio
import pytest

from jobmatch.llm import claude_cli


@pytest.fixture(scope="session")
def anyio_backend():
    """One asyncio loop for the whole session.

    The engine is module-level and its pooled connections belong to the loop
    that opened them; a loop per test would leave each test holding the last
    one's dead connections.
    """
    return "asyncio"


class _FakeClaude:
    """A `claude -p` process that prints what the test gave it; nothing real runs."""

    def __init__(self, out: bytes, returncode: int, err: bytes, hang: bool) -> None:
        self.out, self.returncode, self.err, self.hang = out, returncode, err, hang
        self.argv: tuple[str, ...] = ()
        self.stdin: bytes | None = None
        self.killed = False

    async def __call__(self, *argv, **kwargs):       # stands in for asyncio.create_subprocess_exec
        self.argv = argv
        return self

    async def communicate(self, stdin: bytes) -> tuple[bytes, bytes]:
        self.stdin = stdin
        if self.hang:
            await anyio.sleep_forever()
        return self.out, self.err

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def fake_claude(monkeypatch):
    """Replace `claude -p` with a fake printing `out` (a dict is sent as its JSON)."""
    def install(out, *, returncode: int = 0, err: bytes = b"", hang: bool = False) -> _FakeClaude:
        raw = json.dumps(out).encode() if isinstance(out, dict) else out
        fake = _FakeClaude(raw, returncode, err, hang)
        monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/fake/bin/claude")
        monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", fake)
        return fake

    return install
