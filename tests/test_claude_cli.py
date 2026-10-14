"""`claude -p` through the ledger, with the subprocess faked: one `llm_calls` row per call, ok or not."""
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from jobmatch import cover_note, llm
from jobmatch.db import SessionLocal
from jobmatch.llm import claude_cli
from jobmatch.models import LlmCall, Vacancy

SOURCE_NAME = "test-claude-cli"

pytestmark = pytest.mark.anyio

ANSWER = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "  Dear team, I fit.\n", "session_id": "sess-1", "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 900, "output_tokens": 120},
}


@pytest.fixture
async def vacancy_id():
    async with SessionLocal.begin() as session:
        vacancy = Vacancy(source=SOURCE_NAME, external_id="1",
                          url="https://example.test/vacancy/1", title="Python Developer")
        session.add(vacancy)
        await session.flush()
        made = vacancy.id
    yield made
    async with SessionLocal.begin() as session:
        ids = select(Vacancy.id).where(Vacancy.source == SOURCE_NAME)
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(ids)))
        await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))


async def _rows(vacancy_id: int) -> list[LlmCall]:
    async with SessionLocal() as session:
        return list(await session.scalars(select(LlmCall).where(LlmCall.vacancy_id == vacancy_id)))


async def ask(vacancy_id: int) -> str:
    return await claude_cli.ask("claude-sonnet-5-5", "SYSTEM", "PROMPT", kind="cover_note", vacancy_id=vacancy_id)


async def test_an_answered_call_is_recorded_with_its_usage(vacancy_id, fake_claude):
    fake = fake_claude(ANSWER)
    assert await ask(vacancy_id) == "Dear team, I fit."

    (row,) = await _rows(vacancy_id)
    request = {"model": "claude-sonnet-5-5", "system": "SYSTEM", "prompt": "PROMPT"}
    assert (row.kind, row.status, row.model_requested) == ("cover_note", "ok", "claude-sonnet-5-5")
    assert (row.request, row.inputs_fingerprint) == (request, llm.fingerprint(request))
    assert row.response == ANSWER
    assert (row.response_id, row.input_tokens, row.output_tokens, row.cost_usd) == (
        "sess-1", 900, 120, Decimal("0.0123"))
    assert fake.stdin == b"PROMPT"
    assert fake.argv[fake.argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert fake.argv[fake.argv.index("--system-prompt") + 1] == "SYSTEM"


async def test_an_error_answer_is_recorded_and_raised(vacancy_id, fake_claude):
    failed = {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"}
    fake_claude(failed, returncode=1)
    with pytest.raises(claude_cli.ClaudeError, match="Not logged in"):
        await ask(vacancy_id)

    (row,) = await _rows(vacancy_id)
    assert (row.status, row.error_type, row.response) == ("error", "ClaudeError", failed)
    assert row.cost_usd is None                       # the answer reported none: read with .get()


async def test_a_failed_exit_is_an_error_even_without_is_error(vacancy_id, fake_claude):
    fake_claude({"result": "half", "subtype": "error_during_execution"}, returncode=2)
    with pytest.raises(claude_cli.ClaudeError, match="half"):
        await ask(vacancy_id)
    assert [r.status for r in await _rows(vacancy_id)] == ["error"]


async def test_output_that_is_not_json_is_recorded_with_stderr(vacancy_id, fake_claude):
    fake_claude(b"", returncode=1, err=b"command crashed\n")
    with pytest.raises(claude_cli.ClaudeError, match="exited 1: command crashed"):
        await ask(vacancy_id)
    (row,) = await _rows(vacancy_id)
    assert (row.status, row.response) == ("error", None)


async def test_a_hung_call_is_killed_and_recorded(vacancy_id, fake_claude, monkeypatch):
    monkeypatch.setattr(claude_cli, "TIMEOUT_S", 0.01)
    fake = fake_claude(ANSWER, hang=True)
    with pytest.raises(claude_cli.ClaudeError, match="did not answer within"):
        await ask(vacancy_id)
    assert fake.killed
    assert [r.error_type for r in await _rows(vacancy_id)] == ["ClaudeError"]


async def test_no_claude_on_path_records_nothing(vacancy_id, monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    with pytest.raises(claude_cli.ClaudeError, match="not on PATH"):
        await ask(vacancy_id)
    assert await _rows(vacancy_id) == []


async def test_the_cover_note_is_asked_under_its_kind_and_pinned_model(vacancy_id, fake_claude):
    fake_claude(ANSWER)
    async with SessionLocal() as session:
        vacancy = await session.get_one(Vacancy, vacancy_id)
    assert await cover_note.write(vacancy, None, "cv") == "Dear team, I fit."

    (row,) = await _rows(vacancy_id)
    assert (row.kind, row.model_requested) == ("cover_note", cover_note.MODEL)
    assert row.request["prompt"] == cover_note.build_prompt(vacancy, None, "cv")
