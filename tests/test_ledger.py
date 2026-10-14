"""The LLM ledger: one writer, `llm.recorded`, a spend that counts matching only, and the trace.

The database is real: "the row survives the caller's rollback" is a fact about
two transactions, and only Postgres can show it.
"""
import datetime as dt
from decimal import Decimal

import anyio
import pytest
from sqlalchemy import delete, select

from jobmatch import cli, llm, repository
from jobmatch.db import SessionLocal
from jobmatch.models import LlmCall, Vacancy

SOURCE_NAME = "test-ledger"
COUNTRY = "Ledgerland"  # no real posting is here, so the spend filter isolates these rows

pytestmark = pytest.mark.anyio


@pytest.fixture
async def vacancy_id():
    async with SessionLocal.begin() as session:
        vacancy = Vacancy(
            source=SOURCE_NAME, external_id="1", url="https://example.test/vacancy/1",
            title="Python Developer", country=COUNTRY,
        )
        session.add(vacancy)
        await session.flush()
        made = vacancy.id
    yield made
    async with SessionLocal.begin() as session:
        ids = select(Vacancy.id).where(Vacancy.source == SOURCE_NAME)
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(ids)))
        await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))


def call(vacancy_id: int, kind: str, **values) -> dict:
    return {
        "kind": kind,
        "vacancy_id": vacancy_id,
        "inputs_fingerprint": "f" * 64,
        "model_requested": "jev-1.13" if kind == "match" else "some/model",
        "status": "ok",
        "started_at": dt.datetime.now(dt.UTC),
        **values,
    }


async def test_the_spend_sums_matches_only(vacancy_id):
    await repository.record_call(**call(vacancy_id, "match", cost_usd=Decimal("0.0001")))
    await repository.record_call(**call(vacancy_id, "employer", cost_usd=Decimal("0.0002")))

    async with SessionLocal() as session:
        spend = (await repository.stats(session, country=[COUNTRY]))["spend"]

    count, total, _ = spend
    assert count == 1
    assert total == Decimal("0.0001")


async def test_a_recorded_call_survives_the_callers_rollback(vacancy_id):
    async with SessionLocal() as session:
        (await session.get_one(Vacancy, vacancy_id)).title = "rolled back"
        await session.flush()
        call_id = await repository.record_call(**call(vacancy_id, "match"))
        await session.rollback()

    async with SessionLocal() as session:
        assert (await session.get_one(Vacancy, vacancy_id)).title == "Python Developer"
        assert (await session.get_one(LlmCall, call_id)).kind == "match"


def recorded(vacancy_id: int):
    return llm.recorded(kind="employer", vacancy_id=vacancy_id, fingerprint="f" * 64,
                        model="some/model", request={"model": "some/model"})


async def _row(call_id: int) -> LlmCall:
    async with SessionLocal() as session:
        return await session.get_one(LlmCall, call_id)


async def test_recorded_writes_an_ok_row_with_what_the_caller_attached(vacancy_id):
    usage = llm.Usage(model="some/model-2026", provider="Google", response_id="gen-1",
                      input_tokens=1500, output_tokens=150, cost_usd=Decimal("0.00021"))
    async with recorded(vacancy_id) as call:
        call.response, call.usage = {"id": "gen-1"}, usage

    row = await _row(call.id)
    assert (row.status, row.kind, row.inputs_fingerprint) == ("ok", "employer", "f" * 64)
    assert (row.model_requested, row.request, row.response) == (
        "some/model", {"model": "some/model"}, {"id": "gen-1"})
    assert (row.model_resolved, row.provider, row.response_id) == ("some/model-2026", "Google", "gen-1")
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (1500, 150, Decimal("0.00021"))
    assert row.error_type is None and row.http_status is None
    assert row.duration_ms >= 0 and row.finished_at >= row.started_at


class HttpError(Exception):
    """Shaped like the OpenAI SDK's: a status code and the provider's error body."""

    status_code = 400
    body = {"message": "bad request", "code": 400}


async def test_recorded_writes_an_error_row_with_the_error_body_and_re_raises(vacancy_id):
    with pytest.raises(HttpError):
        async with recorded(vacancy_id) as call:
            raise HttpError("refused")

    row = await _row(call.id)
    assert (row.status, row.http_status, row.error_type, row.error_message) == (
        "error", 400, "HttpError", "refused")
    assert row.response == HttpError.body       # nothing better came back


async def test_recorded_keeps_a_paid_body_over_the_error_body(vacancy_id):
    with pytest.raises(HttpError):
        async with recorded(vacancy_id) as call:
            call.response = {"choices": []}
            raise HttpError("refused")
    assert (await _row(call.id)).response == {"choices": []}


async def test_recorded_still_writes_when_the_call_is_cancelled(vacancy_id):
    """A closed tab cancels the request mid-call; the billed call must still be in the ledger."""
    calls = []

    async def ask():
        async with recorded(vacancy_id) as call:
            calls.append(call)
            await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        tg.start_soon(ask)
        await anyio.wait_all_tasks_blocked()
        tg.cancel_scope.cancel()

    row = await _row(calls[0].id)
    assert (row.status, row.error_type) == ("error", "CancelledError")


async def test_trace_lists_the_newest_calls_and_filters_by_kind(vacancy_id, capsys):
    older = await repository.record_call(**call(vacancy_id, "match", cost_usd=Decimal("0.0001")))
    newer = await repository.record_call(**call(vacancy_id, "employer", input_tokens=1500))

    assert await cli.cmd_trace(None, None, 2) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == [str(newer), str(older)]
    assert "employer" in lines[1] and "1500" in lines[1]
    assert "0.000100000" in lines[2]

    await cli.cmd_trace(None, "match", 1)
    (row,) = capsys.readouterr().out.splitlines()[1:]
    assert row.split()[0] == str(older)


async def test_trace_shows_one_call_in_full(vacancy_id, capsys):
    call_id = await repository.record_call(**call(
        vacancy_id, "employer", status="error",
        error_type="ValidationError", error_message="employer: field required",
        request={"messages": [{"role": "user", "content": "ЭР-Телеком"}]},
        response={"choices": [{"message": {"content": "not json"}}]},
    ))

    assert await cli.cmd_trace(call_id, None, 20) == 0
    out = capsys.readouterr().out
    assert "error_type         ValidationError" in out
    assert "employer: field required" in out
    assert '"content": "ЭР-Телеком"' in out           # ensure_ascii=False
    assert '\n  "choices": [' in out                    # pretty-printed


async def test_trace_of_an_unknown_id_fails(capsys):
    assert await cli.cmd_trace(-1, None, 20) == 1
    assert "no llm_calls row #-1" in capsys.readouterr().out
