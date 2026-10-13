"""Who is behind a posting: the call on a mocked OpenRouter, the homepage check,
the pipeline's cache on the real database, and the API's card.

Nothing here reaches OpenRouter or a live site: the OpenAI SDK runs over an
`httpx2.MockTransport`, and `reachable` gets a mocked transport and IP
literals, so no DNS is asked either. The database is real because the cache is
a unique index and the ledger row is a second transaction.
"""
import asyncio
import dataclasses
import datetime as dt
import json
from decimal import Decimal

import anyio
import httpx2
import openai
import pydantic
import pytest
from sqlalchemy import delete, select

from jobmatch import cli, matching, pipeline, repository, research
from jobmatch.api.app import app
from jobmatch.config import load_settings
from jobmatch.db import SessionLocal
from jobmatch.models import Employer, EmployerIdentity, LlmCall, Vacancy

SOURCE_NAME = "test-research"
MODEL = "google/gemini-2.5-flash-lite"

pytestmark = pytest.mark.anyio

POSTER = research.Poster(
    name="ЭР-Телеком Холдинг",
    site_url="http://rabotavdomru.ru",
    about="Пермь; Телекоммуникации",
    posting="# Python developer\nCompany: ЭР-Телеком Холдинг\n\nМы пишем биллинг.",
)
ANSWER = {
    "employer": "ЭР-Телеком",
    "homepage": "https://ertelecom.ru",
    "careers_page": "http://rabotavdomru.ru",
    "is_agency": False,
    "client": None,
    "notes": "hh links the careers site; the homepage is ertelecom.ru.",
}


def completion(content: str, cost: float | None = 0.00021) -> dict:
    """An OpenRouter chat completion body, as it arrives."""
    usage = {"prompt_tokens": 1500, "completion_tokens": 150, "total_tokens": 1650}
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "gen-123",
        "object": "chat.completion",
        "created": 1760000000,
        "model": MODEL,
        "provider": "Google AI Studio",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": usage,
    }


def key(poster: research.Poster, model: str) -> str:
    """The employer cache key, as the pipeline computes it."""
    return research.fingerprint(research.employer_request(poster, model))


def openrouter(handler,client_class=httpx2.AsyncClient) -> httpx2.AsyncClient:
    """The SDK's HTTP client, answering from `handler` instead of the network."""
    return client_class(transport=httpx2.MockTransport(handler))


def answering(body: dict, sent: list | None = None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return httpx2.Response(200, json=body)
    return handler


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv(research.API_KEY_ENV, "sk-test")


@pytest.fixture
async def vacancy_id():
    """A fetched vacancy whose employer has been looked up."""
    async with SessionLocal.begin() as session:
        session.add(Employer(
            source=SOURCE_NAME, external_id="e-1", url="https://example.invalid/employer/e-1",
            name="ЭР-Телеком Холдинг", site_url="http://rabotavdomru.ru",
            raw={"city": "Пермь", "industries": "Телекоммуникации"},
        ))
        vacancy = Vacancy(
            source=SOURCE_NAME, external_id="v-1", url="https://example.invalid/v-1",
            title="Python developer", company="ЭР-Телеком Холдинг",
            description="Мы пишем биллинг.", employer_external_id="e-1",
            fetched_at=dt.datetime.now(dt.UTC),
        )
        session.add(vacancy)
        await session.flush()
        made = vacancy.id
    yield made
    async with SessionLocal.begin() as session:
        ids = select(Vacancy.id).where(Vacancy.source == SOURCE_NAME)
        await session.execute(delete(EmployerIdentity).where(EmployerIdentity.vacancy_id.in_(ids)))
        await session.execute(delete(LlmCall).where(LlmCall.vacancy_id.in_(ids)))
        await session.execute(delete(Employer).where(Employer.source == SOURCE_NAME))
        await session.execute(delete(Vacancy).where(Vacancy.source == SOURCE_NAME))


async def _call(call_id: int) -> LlmCall:
    async with SessionLocal() as session:
        return await session.get_one(LlmCall, call_id)


async def _only_call(vacancy_id: int) -> LlmCall:
    async with SessionLocal() as session:
        (call,) = (await session.scalars(
            select(LlmCall).where(LlmCall.vacancy_id == vacancy_id)
        )).all()
    return call


# --- the call --------------------------------------------------------------------

async def test_a_good_answer_is_parsed_and_recorded(vacancy_id):
    body = completion(json.dumps(ANSWER, ensure_ascii=False))

    identity, call_id = await research.ask_employer(
        POSTER, MODEL, vacancy_id=vacancy_id, http_client=openrouter(answering(body)),
    )

    assert identity == research.Identity(**ANSWER)
    call = await _call(call_id)
    assert (call.kind, call.status, call.vacancy_id) == ("employer", "ok", vacancy_id)
    assert call.inputs_fingerprint == key(POSTER, MODEL)
    assert call.request == research.employer_request(POSTER, MODEL)
    assert call.response == body
    assert call.cost_usd == Decimal("0.00021")
    assert (call.input_tokens, call.output_tokens) == (1500, 150)
    assert (call.model_resolved, call.provider, call.response_id) == (
        MODEL, "Google AI Studio", "gen-123"
    )
    assert call.duration_ms is not None and call.finished_at is not None


async def test_the_request_asks_for_strict_json_from_providers_that_honour_it(vacancy_id):
    sent: list[dict] = []
    body = completion(json.dumps(ANSWER))

    await research.ask_employer(POSTER, MODEL, vacancy_id=vacancy_id,
                       http_client=openrouter(answering(body, sent)))

    (request,) = sent
    assert request["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "employer_identity", "strict": True,
            "schema": research.Identity.model_json_schema(),
        },
    }
    assert request["provider"] == {"require_parameters": True}
    assert request["usage"] == {"include": True}
    assert request == research.employer_request(POSTER, MODEL)   # what is stored is what was sent


def test_the_schema_is_what_strict_mode_accepts():
    """OpenAI-style `strict`: every property required, nothing extra allowed,
    and "unknown" spelled as a union with null rather than as an optional."""
    schema = research.Identity.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == set(ANSWER)
    for name, prop in schema["properties"].items():
        assert "default" not in prop
        if name != "notes":
            assert {"type": "null"} in prop["anyOf"]


async def test_invalid_json_raises_and_keeps_the_body_it_paid_for(vacancy_id):
    body = completion('{"employer": "ЭР-Телеком"')      # cut off

    with pytest.raises(pydantic.ValidationError):
        await research.ask_employer(POSTER, MODEL, vacancy_id=vacancy_id,
                           http_client=openrouter(answering(body)))

    call = await _only_call(vacancy_id)
    assert (call.kind, call.status, call.error_type) == ("employer", "error", "ValidationError")
    assert call.response == body
    assert call.request == research.employer_request(POSTER, MODEL)
    assert call.cost_usd == Decimal("0.00021")              # paid for all the same


async def test_an_answer_with_extra_fields_is_refused(vacancy_id):
    body = completion(json.dumps({**ANSWER, "ceo": "someone"}))

    with pytest.raises(pydantic.ValidationError):
        await research.ask_employer(POSTER, MODEL, vacancy_id=vacancy_id,
                           http_client=openrouter(answering(body)))

    assert (await _only_call(vacancy_id)).status == "error"


async def test_a_refusal_with_no_content_is_an_error(vacancy_id):
    body = completion("")
    body["choices"][0]["message"] |= {"content": None, "refusal": "I can't help with that."}

    with pytest.raises(ValueError, match="no answer"):
        await research.ask_employer(POSTER, MODEL, vacancy_id=vacancy_id,
                           http_client=openrouter(answering(body)))

    assert (await _only_call(vacancy_id)).response == body


async def test_a_hang_past_the_deadline_is_retried_once_then_recorded(vacancy_id, monkeypatch):
    """OpenRouter's keepalive whitespace keeps a per-read timeout from ever
    firing; only the whole-request deadline ends the wait."""
    monkeypatch.setattr(research, "MODEL_DEADLINE", 0.05)
    attempts = 0

    async def hang(request):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(5)
        return httpx2.Response(200, json=completion(json.dumps(ANSWER)))

    with pytest.raises(openai.APITimeoutError):
        await research.ask_employer(POSTER, MODEL, vacancy_id=vacancy_id,
                           http_client=openrouter(hang, research._Deadline))

    assert attempts == 1 + research.MODEL_RETRIES
    call = await _only_call(vacancy_id)
    assert (call.status, call.error_type) == ("error", "APITimeoutError")
    assert call.request == research.employer_request(POSTER, MODEL)
    assert call.response is None


async def test_an_http_error_stores_openrouters_error_body(vacancy_id):
    error = {"error": {"code": 400, "message": "No endpoints support response_format"}}

    def refuse(request):
        return httpx2.Response(400, json=error)

    with pytest.raises(openai.BadRequestError):
        await research.ask_employer(POSTER, MODEL, vacancy_id=vacancy_id, http_client=openrouter(refuse))

    call = await _only_call(vacancy_id)
    assert (call.status, call.http_status) == ("error", 400)
    assert call.response == error["error"]     # the SDK unwraps `error`


async def test_without_a_key_nothing_is_sent_or_recorded(vacancy_id, monkeypatch):
    monkeypatch.delenv(research.API_KEY_ENV)

    with pytest.raises(ValueError, match=research.API_KEY_ENV):
        await research.ask_employer(POSTER, MODEL, vacancy_id=vacancy_id,
                           http_client=openrouter(lambda r: pytest.fail("sent")))

    async with SessionLocal() as session:
        assert not (await session.scalars(
            select(LlmCall).where(LlmCall.vacancy_id == vacancy_id)
        )).all()


@pytest.mark.parametrize("change", [
    {"name": "ЭР-Телеком"},
    {"site_url": "https://ertelecom.ru"},
    {"about": "Пермь"},
    {"posting": POSTER.posting + " Удалённо."},
])
def test_the_fingerprint_changes_with_each_input(change):
    changed = dataclasses.replace(POSTER, **change)
    assert key(changed, MODEL) != key(POSTER, MODEL)


def test_the_fingerprint_is_stable_and_changes_with_the_model():
    assert key(POSTER, MODEL) == key(POSTER, MODEL)
    assert key(POSTER, "mistralai/mistral-small") != key(POSTER, MODEL)


def test_the_employer_fingerprint_is_pinned():
    """Pinned on 2026-10-13, when `Identity`'s docstring was rewritten for the
    model. A change here makes every stored `employer_identities` row miss the
    cache -- that docstring, which is in the schema, is the easy way to cause
    one. Change the pin only on purpose."""
    assert key(POSTER, MODEL) == (
        "d2fdbcbc0761aab2429fdfe17c89fbbb1e34fe49fc83004b1e228f13a62eef27"
    )


@pytest.mark.parametrize(("given", "stored"), [
    ("ertelecom.ru", "https://ertelecom.ru"),
    ("rabotavdomru.ru/vacancies", "https://rabotavdomru.ru/vacancies"),
    ("http://rabotavdomru.ru", "http://rabotavdomru.ru"),
    (None, None),
])
def test_a_bare_domain_is_stored_as_an_https_url(given, stored):
    identity = research.Identity(**{**ANSWER, "homepage": given, "careers_page": given})
    assert identity.homepage == identity.careers_page == stored


@pytest.mark.parametrize("given", [
    "javascript://x%0aalert(1)",      # has "://", so no scheme is added to it
    "file:///etc/passwd",
    "ftp://ertelecom.ru",
])
def test_a_link_that_is_not_http_is_stored_as_unknown(given):
    """The card links these; a `javascript:` URL would run on click."""
    identity = research.Identity(**{**ANSWER, "homepage": given, "careers_page": given})
    assert identity.homepage is identity.careers_page is None


def test_the_url_fix_leaves_the_schema_alone():
    """The schema is in the fingerprint; a validator must not change it."""
    homepage = research.Identity.model_json_schema()["properties"]["homepage"]
    assert homepage["anyOf"] == [{"type": "string"}, {"type": "null"}]


async def test_a_bare_domain_answer_is_stored_with_its_scheme_and_kept_raw(vacancy_id):
    body = completion(json.dumps({**ANSWER, "homepage": "ertelecom.ru"}))

    identity, call_id = await research.ask_employer(
        POSTER, MODEL, vacancy_id=vacancy_id, http_client=openrouter(answering(body)),
    )

    assert identity.homepage == "https://ertelecom.ru"
    assert (await _call(call_id)).response == body      # what the model really said


# --- does the homepage answer ------------------------------------------------------

PUBLIC = "http://93.184.215.14"      # an IP literal: no DNS asked


def site(routes: dict[str, httpx2.Response]) -> httpx2.AsyncClient:
    """A mocked web: each URL answers what `routes` says; anything else fails."""
    def handler(request):
        url = str(request.url)
        if url not in routes:
            pytest.fail(f"requested {url}")
        return routes[url]
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_a_public_page_that_answers_is_reachable():
    assert await research.reachable(PUBLIC + "/", http_client=site({
        PUBLIC + "/": httpx2.Response(200, text="<html>hello</html>"),
    }))


async def test_a_missing_page_is_not_reachable():
    assert not await research.reachable(PUBLIC + "/", http_client=site({
        PUBLIC + "/": httpx2.Response(404),
    }))


@pytest.mark.parametrize("url", [
    "http://localhost:8000/api/vacancies",
    "http://127.0.0.1/",
    "http://10.1.2.3/",
    "http://192.168.0.1/",
    "http://[::1]/",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "ertelecom.ru",                   # no scheme
])
async def test_private_addresses_and_other_schemes_are_refused_unasked(url):
    assert not await research.reachable(url, http_client=site({}))


async def test_redirects_are_followed_and_each_hop_checked():
    assert await research.reachable(PUBLIC + "/", http_client=site({
        PUBLIC + "/": httpx2.Response(301, headers={"location": "/home"}),
        PUBLIC + "/home": httpx2.Response(200),
    }))
    assert not await research.reachable(PUBLIC + "/", http_client=site({
        PUBLIC + "/": httpx2.Response(302, headers={"location": "http://127.0.0.1:8000/"}),
    }))


async def test_too_many_redirects_is_not_reachable():
    loop = {
        f"{PUBLIC}/{i}": httpx2.Response(302, headers={"location": f"/{i + 1}"})
        for i in range(research.MAX_REDIRECTS + 1)
    }
    assert not await research.reachable(PUBLIC + "/0", http_client=site(loop))


async def test_a_connection_failure_is_not_reachable():
    def down(request):
        raise httpx2.ConnectError("refused", request=request)
    assert not await research.reachable(
        PUBLIC + "/", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(down))
    )


# --- the pipeline: the cache, on the real database -----------------------------------

@pytest.fixture
def model_calls(monkeypatch):
    """`research.ask_employer` and `reachable` replaced; returns the posters asked.

    The fake still writes its ledger row, because `employer_identities`
    points at one.
    """
    asked: list[research.Poster] = []

    async def ask(poster, model, *, vacancy_id, http_client=None):
        asked.append(poster)
        call_id = await repository.record_call(
            kind="employer", vacancy_id=vacancy_id, model_requested=model,
            inputs_fingerprint=key(poster, model), status="ok",
            cost_usd=Decimal("0.0002"), started_at=dt.datetime.now(dt.UTC),
        )
        return research.Identity(**ANSWER), call_id

    async def reachable(url, *, http_client=None):
        return url == ANSWER["homepage"]

    monkeypatch.setattr(research, "ask_employer", ask)
    monkeypatch.setattr(research, "reachable", reachable)
    return asked


async def test_identifying_saves_the_answer_and_its_homepage_check(vacancy_id, model_calls):
    row = await pipeline.identify_employer(vacancy_id, MODEL)

    assert row.answer == ANSWER
    assert row.homepage_ok is True
    assert row.careers_ok is False          # the fake answers for the homepage only
    async with SessionLocal() as session:
        latest = await repository.latest_identity(session, vacancy_id)
    assert latest.id == row.id
    assert latest.inputs_fingerprint == key(model_calls[0], MODEL)


async def test_the_same_request_twice_is_answered_from_the_database(vacancy_id, model_calls):
    first = await pipeline.identify_employer(vacancy_id, MODEL)
    second = await pipeline.identify_employer(vacancy_id, MODEL)

    assert second.id == first.id
    assert len(model_calls) == 1


async def test_another_model_is_another_request(vacancy_id, model_calls):
    first = await pipeline.identify_employer(vacancy_id, MODEL)
    other = await pipeline.identify_employer(vacancy_id, "mistralai/mistral-small")

    assert other.id != first.id
    assert len(model_calls) == 2
    async with SessionLocal() as session:
        assert (await repository.latest_identity(session, vacancy_id)).id == other.id


async def test_the_poster_is_the_hh_page_and_the_posting_as_jev_reads_it(vacancy_id, model_calls):
    await pipeline.identify_employer(vacancy_id, MODEL)

    (poster,) = model_calls
    async with SessionLocal() as session:
        vacancy = await session.get_one(Vacancy, vacancy_id)
    assert poster == research.Poster(
        name="ЭР-Телеком Холдинг",
        site_url="http://rabotavdomru.ru",
        about="Пермь; Телекоммуникации",
        posting=matching.job_text(vacancy),
    )


async def test_without_a_looked_up_employer_there_is_nothing_to_ask(vacancy_id, model_calls):
    async with SessionLocal.begin() as session:
        await session.execute(delete(Employer).where(Employer.source == SOURCE_NAME))

    with pytest.raises(pipeline.NotLookedUp, match="looked up"):
        await pipeline.identify_employer(vacancy_id, MODEL)
    assert model_calls == []


async def test_an_unknown_vacancy_is_employer_unknown(model_calls):
    with pytest.raises(pipeline.EmployerUnknown, match="no vacancy"):
        await pipeline.identify_employer(0, MODEL)


async def test_saving_the_same_fingerprint_twice_keeps_the_first_row(vacancy_id):
    call_id = await repository.record_call(
        kind="employer", vacancy_id=vacancy_id, model_requested=MODEL,
        inputs_fingerprint="a" * 64, status="ok", started_at=dt.datetime.now(dt.UTC),
    )
    async with SessionLocal.begin() as session:
        first = await repository.save_identity(
            session, vacancy_id, "a" * 64, ANSWER, True, None, call_id
        )
    async with SessionLocal.begin() as session:
        again = await repository.save_identity(
            session, vacancy_id, "a" * 64, {**ANSWER, "notes": "later"}, False, None, call_id
        )
    assert again.id == first.id
    assert again.answer["notes"] == ANSWER["notes"]


async def test_the_command_says_whether_it_paid(vacancy_id, model_calls, capsys):
    assert await cli.cmd_identify("config.toml", vacancy_id) == 0
    out = capsys.readouterr().out
    assert "employer       ЭР-Телеком" in out
    assert "client         unknown" in out
    assert "homepage_ok    True" in out
    assert "careers_ok     False" in out
    assert "paid: llm_calls #" in out and "$0.000200000" in out

    assert await cli.cmd_identify("config.toml", vacancy_id) == 0
    assert "cached, free: answered earlier by llm_calls #" in capsys.readouterr().out


async def test_an_answer_paid_for_is_saved_even_if_the_click_is_cancelled(
    vacancy_id, model_calls, monkeypatch
):
    """A closed tab cancels the request mid-check; the next click must not pay."""
    checking = anyio.Event()

    async def slow_reachable(url, *, http_client=None):
        checking.set()
        await anyio.sleep(0.1)
        return True

    monkeypatch.setattr(research, "reachable", slow_reachable)
    async with anyio.create_task_group() as tg:
        tg.start_soon(pipeline.identify_employer, vacancy_id, MODEL)
        await checking.wait()
        tg.cancel_scope.cancel()

    async with SessionLocal() as session:
        saved = await repository.latest_identity(session, vacancy_id)
    assert saved is not None and saved.homepage_ok is True
    await pipeline.identify_employer(vacancy_id, MODEL)
    assert len(model_calls) == 1


@pytest.mark.parametrize("error", [
    pipeline.NotLookedUp("the company has not been looked up yet"),
    pipeline.EmployerUnknown("this posting does not name its employer"),
])
async def test_the_command_reports_why_it_cannot_identify(vacancy_id, monkeypatch, capsys, error):
    async def fails(*args):
        raise error

    monkeypatch.setattr(pipeline, "identify_employer", fails)
    assert await cli.cmd_identify("config.toml", vacancy_id) == 1
    assert str(error) in capsys.readouterr().out


# --- the API: the card's two requests, with the call stubbed ------------------------

@pytest.fixture
async def client():
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_the_identity_route_returns_the_answer_and_what_it_cost(
    client, vacancy_id, model_calls
):
    resp = await client.post(f"/api/vacancies/{vacancy_id}/company/identity")

    assert resp.status_code == 200
    body = resp.json()
    assert {k: body[k] for k in ANSWER} == ANSWER
    assert body["homepage_ok"] is True and body["careers_ok"] is False
    # the fake records no model_resolved, so the model asked for is shown
    assert body["model"] == load_settings().research_model
    assert body["cost_usd"] == 0.0002
    assert body["llm_call_id"] == (await _only_call(vacancy_id)).id
    assert body["created_at"]


async def test_the_company_carries_its_newest_identity(client, vacancy_id, model_calls, monkeypatch):
    assert (await client.get(f"/api/vacancies/{vacancy_id}/company")).json()["identity"] is None
    posted = (await client.post(f"/api/vacancies/{vacancy_id}/company/identity")).json()

    got = (await client.get(f"/api/vacancies/{vacancy_id}/company")).json()
    assert got["name"] == "ЭР-Телеком Холдинг"
    assert got["identity"] == posted

    # a fresh lookup keeps showing the answer already stored, and asks nothing
    async def lookup(vid):
        async with SessionLocal() as session:
            return await repository.employer_of(session, vid)
    monkeypatch.setattr(pipeline, "lookup_employer", lookup)
    looked_up = (await client.post(f"/api/vacancies/{vacancy_id}/company")).json()
    assert looked_up["identity"] == posted
    assert len(model_calls) == 1


async def test_identifying_before_the_lookup_answers_409(client, vacancy_id, model_calls):
    async with SessionLocal.begin() as session:
        await session.execute(delete(Employer).where(Employer.source == SOURCE_NAME))

    resp = await client.post(f"/api/vacancies/{vacancy_id}/company/identity")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "look up the company first"
    assert model_calls == []


async def test_identifying_an_unknown_vacancy_answers_404(client, model_calls):
    assert (await client.post("/api/vacancies/0/company/identity")).status_code == 404


async def test_without_a_key_the_identity_route_answers_503_unasked(
    client, vacancy_id, model_calls, monkeypatch
):
    monkeypatch.delenv(research.API_KEY_ENV)

    resp = await client.post(f"/api/vacancies/{vacancy_id}/company/identity")

    assert resp.status_code == 503
    assert research.API_KEY_ENV in resp.json()["detail"]
    assert model_calls == []


async def test_a_failed_call_answers_502_with_the_reason(client, vacancy_id, monkeypatch):
    async def ask(*args, **kwargs):
        raise ValueError("the response holds no answer")
    monkeypatch.setattr(research, "ask_employer", ask)

    resp = await client.post(f"/api/vacancies/{vacancy_id}/company/identity")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "ValueError: the response holds no answer"



# --- who recruits for it: the search and its citation check -------------------------

TARGET = research.Target(name="ЭР-Телеком", homepage="https://ertelecom.ru", city="Пермь")
ONLINE = MODEL + ":online"
PERSON = {
    "name": "Иван Петров",
    "role": "IT Recruiter",
    "linkedin_url": "https://ru.linkedin.com/in/ivan-petrov-123/",
    "evidence": "Иван Петров – IT Recruiter – ЭР-Телеком | LinkedIn",
}
GUESSED = {**PERSON, "name": "Анна Смирнова", "linkedin_url": "https://www.linkedin.com/in/anna-smirnova"}


def citation(url: str, title: str | None = "Иван Петров – IT Recruiter – ЭР-Телеком") -> dict:
    """An OpenRouter web-search annotation, as it arrives."""
    return {"type": "url_citation",
            "url_citation": {"url": url, "title": title, "content": "...",
                             "start_index": 0, "end_index": 10}}


def searched(people: list[dict], annotations: list[dict]) -> dict:
    """A completion from an `:online` model: the answer, and what the search cited."""
    body = completion(json.dumps({"people": people, "notes": "searched LinkedIn"},
                                 ensure_ascii=False))
    body["choices"][0]["message"]["annotations"] = annotations
    return body


def answer_of(*people: dict) -> research.Recruiters:
    return research.Recruiters(people=[research.Person(**p) for p in people], notes="")


@pytest.mark.parametrize(("url", "slug"), [
    ("https://ru.linkedin.com/in/ivan-petrov-123/", "ivan-petrov-123"),
    ("https://www.linkedin.com/in/Ivan-Petrov-123?trk=public_profile", "ivan-petrov-123"),
    ("http://linkedin.com/in/ivan-petrov-123/details/experience/", "ivan-petrov-123"),
    ("https://www.linkedin.com/in/%D0%B8%D0%B2%D0%B0%D0%BD", "иван"),
    ("https://www.linkedin.com/in/Иван", "иван"),
])
def test_a_linkedin_profile_has_a_slug(url, slug):
    assert research.linkedin_slug(url) == slug


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/company/ertelecom",
    "https://www.linkedin.com/pub/ivan-petrov/1/2/3",
    "https://www.linkedin.com/in/",
    "https://www.linkedin.com/in/..",
    "https://www.linkedin.com/in/.",
    "https://www.linkedin.com/in/%20",
    "https://www.linkedin.com/in/a%2Fb",
    "javascript:alert('linkedin.com/in/x')",
    "https://linkedin.com.evil.io/in/x",
    "https://notlinkedin.com/in/x",
    "linkedin.com/in/x",                      # no scheme
    "https://[::1/in/x",                      # malformed
    "",
])
def test_anything_else_has_none(url):
    assert research.linkedin_slug(url) is None


def test_a_cited_profile_is_kept_in_its_canonical_form():
    body = searched([], [citation("https://ru.linkedin.com/in/Ivan-Petrov-123?trk=x")])

    confirmed = research.keep_cited(answer_of(PERSON), body)

    assert confirmed == research.Confirmed(
        people=(research.ConfirmedPerson(
            name="Иван Петров", role="IT Recruiter",
            linkedin_url="https://www.linkedin.com/in/ivan-petrov-123",
            evidence=PERSON["evidence"],
            source_title="Иван Петров – IT Recruiter – ЭР-Телеком",
        ),),
        dropped=0, sources=1,
    )


def test_an_uncited_profile_is_dropped():
    body = searched([], [citation(PERSON["linkedin_url"]),
                         citation("https://ertelecom.ru/careers", "Карьера")])

    confirmed = research.keep_cited(answer_of(PERSON, GUESSED), body)

    assert [p.name for p in confirmed.people] == ["Иван Петров"]
    assert (confirmed.dropped, confirmed.sources) == (1, 2)


def test_a_cyrillic_profile_matches_its_percent_encoded_citation():
    person = {**PERSON, "linkedin_url": "https://www.linkedin.com/in/Иван"}
    body = searched([], [citation("https://www.linkedin.com/in/%D0%B8%D0%B2%D0%B0%D0%BD")])

    (kept,) = research.keep_cited(answer_of(person), body).people

    assert kept.linkedin_url == "https://www.linkedin.com/in/%D0%B8%D0%B2%D0%B0%D0%BD"


def test_one_person_named_twice_is_kept_once():
    body = searched([], [citation(PERSON["linkedin_url"])])

    confirmed = research.keep_cited(answer_of(PERSON, PERSON), body)

    assert (len(confirmed.people), confirmed.dropped) == (1, 0)


def test_without_citations_everyone_is_dropped():
    """A model that did not search, or whose citations did not arrive, names
    people from memory; none of them is shown."""
    confirmed = research.keep_cited(answer_of(PERSON, GUESSED), completion("{}"))

    assert confirmed == research.Confirmed(people=(), dropped=2, sources=0)


def test_a_url_only_in_the_content_is_not_a_citation():
    body = searched([PERSON], [])
    assert research.cited(body) == {}


@pytest.mark.parametrize("body", [
    {},
    {"choices": []},
    {"choices": None},
    {"choices": [{"message": None}]},
    {"choices": [{"message": {"annotations": "x"}}]},
    {"choices": [{"message": {"annotations": [None, 1, {"type": "url_citation"},
                                              {"type": "url_citation", "url_citation": "x"},
                                              {"type": "url_citation",
                                               "url_citation": {"url": 5, "title": 7}},
                                              {"type": "file", "url": PERSON["linkedin_url"]}]}}]},
])
def test_a_malformed_body_drops_everyone_and_does_not_raise(body):
    confirmed = research.keep_cited(answer_of(PERSON), body)
    assert (confirmed.people, confirmed.dropped) == ((), 1)


def test_a_citation_without_a_title_still_confirms():
    body = searched([], [citation(PERSON["linkedin_url"], title=None)])
    (kept,) = research.keep_cited(answer_of(PERSON), body).people
    assert kept.source_title is None


@pytest.mark.parametrize(("target", "prompt"), [
    (TARGET, "Recruiters, HR or talent acquisition at ЭР-Телеком "
             "(ertelecom.ru, Пермь) — LinkedIn profiles"),
    (research.Target("ЭР-Телеком", "https://www.ertelecom.ru/about", None),
     "Recruiters, HR or talent acquisition at ЭР-Телеком (ertelecom.ru) — LinkedIn profiles"),
    (research.Target("ЭР-Телеком", None, "Пермь"),
     "Recruiters, HR or talent acquisition at ЭР-Телеком (Пермь) — LinkedIn profiles"),
    (research.Target("ЭР-Телеком", None, None),
     "Recruiters, HR or talent acquisition at ЭР-Телеком — LinkedIn profiles"),
])
def test_the_question_is_short_like_a_search_query(target, prompt):
    assert research.recruiters_prompt(target) == prompt


def test_the_recruiters_schema_is_what_strict_mode_accepts():
    recruiters = research.Recruiters.model_json_schema()
    for schema in (recruiters, recruiters["$defs"]["Person"]):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert all("default" not in prop for prop in schema["properties"].values())


def test_the_recruiters_schema_holds_no_notes_for_people():
    """Every description is sent to the model and paid for in the fingerprint."""
    text = json.dumps(research.recruiters_request(TARGET, ONLINE)["response_format"]).lower()
    for word in ("test", "pydantic", "openai", "fingerprint", "keep_cited", "todo"):
        assert word not in text


def test_the_search_is_the_model_id_not_a_plugin():
    request = research.recruiters_request(TARGET, ONLINE)
    assert "plugins" not in request
    assert request["model"] == ONLINE


async def test_the_recruiters_search_is_sent_as_stored_and_recorded(vacancy_id):
    sent: list[dict] = []
    body = searched([PERSON, GUESSED], [citation(PERSON["linkedin_url"])])

    confirmed, call_id = await research.ask_recruiters(
        TARGET, ONLINE, vacancy_id=vacancy_id, http_client=openrouter(answering(body, sent)),
    )

    (request,) = sent
    assert request == research.recruiters_request(TARGET, ONLINE)
    assert ([p.name for p in confirmed.people], confirmed.dropped) == (["Иван Петров"], 1)
    call = await _call(call_id)
    assert (call.kind, call.status, call.vacancy_id) == ("recruiters", "ok", vacancy_id)
    assert call.request == request
    assert call.response == body                 # the citations kept with the answer
    assert call.inputs_fingerprint == research.fingerprint(request)


async def test_a_recruiters_search_past_the_deadline_is_recorded(vacancy_id, monkeypatch):
    monkeypatch.setattr(research, "MODEL_DEADLINE", 0.05)

    async def hang(request):
        await asyncio.sleep(5)
        return httpx2.Response(200, json=searched([], []))

    with pytest.raises(openai.APITimeoutError):
        await research.ask_recruiters(TARGET, ONLINE, vacancy_id=vacancy_id,
                                      http_client=openrouter(hang, research._Deadline))

    call = await _only_call(vacancy_id)
    assert (call.kind, call.status, call.error_type) == ("recruiters", "error", "APITimeoutError")
    assert call.request == research.recruiters_request(TARGET, ONLINE)
