"""The command line: thin coroutines over the library; ``main`` is the one place a loop starts."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import logging
import sys
from collections.abc import Coroutine
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import company_research, matching, pipeline, repository
from .config import DEFAULT_PATH, Settings, load_settings
from .db import SessionLocal, engine
from .models import LlmCall
from .sources import source_for_url

log = logging.getLogger(__name__)


async def cmd_run(config_path: str, limit: int | None, match: bool = True,
            only: str | None = None) -> int:
    settings = load_settings(config_path)
    if only:
        settings = settings.only(only)
    report = await pipeline.run(settings, limit=limit, match=match)
    async with SessionLocal() as session:
        stored, fetched, matched = await repository.count_vacancies(session)
    print(f"\n{report.summary()}")
    print(
        f"database now holds {stored} vacancies, {fetched} fetched, "
        f"{matched} with a current match"
    )
    for url, exc in report.failures:
        print(f"  ! {url}: {type(exc).__name__}: {exc}")
    return 0


async def cmd_discover(config_path: str, only: str | None, limit: int | None) -> int:
    """No pages pulled, nothing spent."""
    settings = load_settings(config_path)
    if only:
        settings = settings.only(only)
    report = await pipeline.discover_all(settings, limit=limit)
    async with SessionLocal() as session:
        stored, fetched, matched = await repository.count_vacancies(session)
        queued = len(await repository.fetch_queue(session, [e.name for e in settings.sources]))
    print(f"\ndiscovered {report.discovered} listings, {report.added} new, "
          f"failed {len(report.failures)}")
    print(f"database now holds {stored} vacancies, {fetched} fetched; "
          f"{queued} queued for `jobmatch fetch --pending`")
    for url, exc in report.failures:
        print(f"  ! {url}: {type(exc).__name__}: {exc}")
    return 0


async def cmd_fetch(url: str) -> int:
    source = source_for_url(url)
    data = await source.fetch(url)
    async with SessionLocal.begin() as session:
        vacancy = await repository.store_fetched(session, source.name, data)
        print(
            f"#{vacancy.id}  {vacancy.source}:{vacancy.external_id}\n"
            f"  title       {data.title}\n"
            f"  company     {data.company}\n"
            f"  salary      {data.salary}\n"
            f"  experience  {data.experience}\n"
            f"  published   {data.published_at}\n"
            f"  skills      {', '.join(data.skills) or '-'}\n"
            f"  description {len(data.description)} chars\n"
            f"  raw keys    {', '.join(data.raw) or '-'}"
        )
    return 0


async def cmd_match(config_path: str, *, dry_run: bool, cv: str | None,
              country: str | None, limit: int | None) -> int:
    settings = load_settings(config_path)
    if cv:
        settings = dataclasses.replace(settings, cv_path=Path(cv))
    if dry_run:
        return await _dry_run(settings, country, limit)

    report = await pipeline.match_all(settings, limit=limit, country=country)
    print(f"\nmatched {report.matched} (${report.cost:.6f}), "
          f"already matched {report.already_matched}, failed {len(report.failures)}")
    for url, exc in report.failures:
        print(f"  ! {url}: {type(exc).__name__}: {exc}")
    return 0


async def _dry_run(settings: Settings, country: str | None, limit: int | None) -> int:
    """Writes nothing, so the spend printed here is in no ledger."""
    async with SessionLocal() as session:
        vacancy_ids = (await repository.matchable_vacancy_ids(
            session, [entry.name for entry in settings.sources], country
        ))[: limit or 10]
        vacancies = await repository.vacancies_by_ids(session, vacancy_ids)
        stored = await repository.current_matches(session, vacancy_ids)

    cv = matching.load_cv(settings.cv_path)
    print(f"dry run — {settings.cv_path} ({len(cv)} chars), model {settings.model_for('match')}, "
          f"{len(vacancies)} vacancies. Nothing will be written.\n")

    total = Decimal(0)
    async with matching.open_client() as client:
        for vacancy in vacancies:          # one at a time, so the output reads in order
            preview = await matching.preview_vacancy(
                client, cv, vacancy, settings.model_for("match"), job=matching.job_text(vacancy)
            )
            total += preview.cost_usd or Decimal(0)
            was = stored.get(vacancy.id)
            before = f"{was.overall_fit_label} {was.overall_fit_score:.2f}" if was else "—"
            delta = ""
            if was:
                change = preview.overall_fit_score - was.overall_fit_score
                delta = f"  {change:+.2f}" if change else "  ="
            print(f"{(vacancy.title or '')[:46]:46} {before:>15} -> "
                  f"{preview.overall_fit_label} {preview.overall_fit_score:.2f}{delta}")
            print(f"{'':46} confidence {preview.overall_fit_confidence:.2f}  "
                  f"qualified {preview.is_qualified_noul:.2f}  gap {preview.top_gap}  "
                  f"angle {preview.best_angle} ({preview.best_angle_confidence:.2f})")

    print(f"\n{len(vacancies)} calls, ${total:.6f} — not recorded in llm_calls")
    return 0


async def cmd_cv_hash(config_path: str, cv: str | None) -> int:
    """The hash a CV's answers are stored under, after sanitising, as `stats_cv_hash` wants."""
    settings = load_settings(config_path)
    path = Path(cv) if cv else settings.cv_path
    digest = matching.sha256(matching.load_cv(path))
    async with SessionLocal() as session:
        current = await repository.count_current_matches(session, digest)
    print(f"{digest}  {path}  ({current} current matches)")
    return 0


async def cmd_fetch_pending(config_path: str, limit: int | None) -> int:
    """No crawling, no spending."""
    settings = load_settings(config_path)
    report = await pipeline.fetch_all(settings, limit=limit)
    async with SessionLocal() as session:
        stored, fetched, matched = await repository.count_vacancies(session)
        remaining = len(await repository.fetch_queue(session, [e.name for e in settings.sources]))
    print(f"\n{report.summary()}")
    print(f"database now holds {stored} vacancies, {fetched} fetched; {remaining} still queued")
    for url, exc in report.failures[:10]:
        print(f"  ! {url}: {type(exc).__name__}: {exc}")
    if len(report.failures) > 10:
        print(f"  ... and {len(report.failures) - 10} more")
    return 0


async def cmd_identify(config_path: str, vacancy_id: int) -> int:
    """Pays unless the same request was answered before."""
    settings = load_settings(config_path)
    try:
        await _look_up_first(vacancy_id)
        started = dt.datetime.now(dt.UTC)
        row = await company_research.identify_employer(vacancy_id, settings.model_for("employer"))
    except (company_research.EmployerUnknown, company_research.NotLookedUp) as e:
        print(f"cannot identify the employer of vacancy {vacancy_id}: {e}")
        return 1
    async with SessionLocal() as session:
        call = await repository.get_call(session, row.llm_call_id)

    for name, value in row.answer.items():
        print(f"{name:14} {'unknown' if value is None else value}")
    for name in ("homepage_ok", "careers_ok"):
        print(f"{name:14} {_or_dash(getattr(row, name))}")
    # A stored answer was paid for by an earlier call; both clocks are Python's.
    cached = call.started_at < started
    print(f"\n{'cached, free: answered earlier by' if cached else 'paid:'} "
          f"llm_calls #{call.id}, {call.model_requested}, ${_or_dash(call.cost_usd)}")
    return 0


async def _look_up_first(vacancy_id: int) -> None:
    """One free hh page, as the UI's Look up company button."""
    async with SessionLocal() as session:
        looked_up = await repository.employer_of(session, vacancy_id) is not None
    if not looked_up:
        print("looking the company up on hh first")
        await company_research.lookup_employer(vacancy_id)


async def cmd_recruiters(config_path: str, vacancy_id: int, *, refresh: bool = False) -> int:
    """Pays unless a fresh search with the same request exists; with `refresh`, always."""
    settings = load_settings(config_path)
    try:
        await _look_up_first(vacancy_id)
        started = dt.datetime.now(dt.UTC)
        row = await company_research.find_recruiters(
            vacancy_id, settings.model_for("recruiters"), refresh=refresh
        )
    except (company_research.EmployerUnknown, company_research.NotLookedUp) as e:
        print(f"cannot search for the recruiters of vacancy {vacancy_id}: {e}")
        return 1
    async with SessionLocal() as session:
        call = await repository.get_call(session, row.llm_call_id)

    answer = row.answer
    for person in answer["people"]:
        print(" · ".join(_or_dash(person[key])
                         for key in ("name", "role", "linkedin_url", "source_title")))
    print(f"{len(answer['people'])} found · {answer['dropped']} dropped · "
          f"{answer['sources']} sources")
    # As in cmd_identify: a reused search was paid for by an earlier call.
    cached = call.started_at < started
    print(f"\n{'cached, free: searched earlier by' if cached else 'paid:'} "
          f"llm_calls #{call.id}, {call.model_requested}, ${_or_dash(call.cost_usd)}")
    return 0


async def cmd_trace(call_id: int | None, kind: str | None, last: int) -> int:
    """The last calls as a table, or one call in full; read-only and free."""
    if call_id is not None:
        async with SessionLocal() as session:
            call = await repository.get_call(session, call_id)
        if call is None:
            print(f"no llm_calls row #{call_id}")
            return 1
        _print_call(call)
        return 0

    async with SessionLocal() as session:
        calls = await repository.recent_calls(session, kind=kind, limit=last)
    _print_calls(calls)
    return 0


def _print_calls(calls: list[LlmCall]) -> None:
    print(f"{'id':>7}  {'started':16}  {'kind':10} {'model':30} {'status':6} "
          f"{'in':>6} {'out':>5} {'cost $':>11} {'ms':>6}  vacancy")
    for c in calls:
        print(f"{c.id:>7}  {c.started_at:%Y-%m-%d %H:%M}  {c.kind:10} "
              f"{c.model_requested[:30]:30} {c.status:6} "
              f"{_or_dash(c.input_tokens):>6} {_or_dash(c.output_tokens):>5} "
              f"{_or_dash(c.cost_usd):>11} {_or_dash(c.duration_ms):>6}  "
              f"{_or_dash(c.vacancy_id)}")


def _print_call(call: LlmCall) -> None:
    # Every column, so one added later shows up without an edit.
    for column in LlmCall.__table__.columns:
        name = column.name
        if name in ("request", "response"):
            continue
        value = getattr(call, name)
        if name.startswith("error_") and value is None:
            continue          # a successful call has no error to show
        print(f"{name:18} {_or_dash(value)}")
    for name in ("request", "response"):
        value = getattr(call, name)
        print(f"\n{name}:")
        print(json.dumps(value, ensure_ascii=False, indent=2) if value is not None else "  -")


def _or_dash(value: object) -> str:
    return "-" if value is None else str(value)


def _positive_int(text: str) -> int:
    number = int(text)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {number}")
    return number


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="jobmatch")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="discover, fetch and match the configured filter")
    run.add_argument("--config", default=str(DEFAULT_PATH))
    run.add_argument(
        "--limit",
        type=int,
        help="pay for at most this many matches — try a change cheaply first",
    )
    run.add_argument(
        "--no-match",
        action="store_true",
        help="crawl and fetch only, spend nothing; `jobmatch match` pays later",
    )
    run.add_argument(
        "--only",
        metavar="SWEEP",
        help="crawl only this sweep, e.g. --only bg; omit it to crawl them all",
    )

    discover = sub.add_parser(
        "discover", help="crawl and store listings only — no pages, no spending"
    )
    discover.add_argument("--config", default=str(DEFAULT_PATH))
    discover.add_argument(
        "--only", metavar="SWEEP", help="crawl only this sweep, e.g. --only bg"
    )
    discover.add_argument(
        "--limit",
        type=int,
        help="stop after this many listings — try a search change for one page",
    )

    fetch = sub.add_parser("fetch", help="scrape vacancy pages into the database")
    fetch.add_argument(
        "url",
        nargs="?",
        help="one vacancy URL; omit it with --pending to drain the fetch queue",
    )
    fetch.add_argument(
        "--pending",
        action="store_true",
        help="fetch every stored vacancy still missing its page, without crawling",
    )
    fetch.add_argument("--config", default=str(DEFAULT_PATH))
    fetch.add_argument("--limit", type=int, help="at most this many pages")

    cv_hash = sub.add_parser(
        "cv-hash", help="print the hash a CV's stored answers live under"
    )
    cv_hash.add_argument("cv", nargs="?", help="a CV file; default is the configured one")
    cv_hash.add_argument("--config", default=str(DEFAULT_PATH))

    match = sub.add_parser("match", help="match stored vacancies, without crawling")
    match.add_argument("--config", default=str(DEFAULT_PATH))
    match.add_argument(
        "--dry-run",
        action="store_true",
        help="print what Jev answers and write nothing — the spend is then not in llm_calls",
    )
    match.add_argument("--cv", help="use this CV instead of the configured one")
    match.add_argument("--country", help="only vacancies in this country")
    match.add_argument("--limit", type=int, help="at most this many vacancies (dry run: 10)")

    trace = sub.add_parser(
        "trace", help="read the LLM ledger: the last calls, or one in full — free"
    )
    trace.add_argument("id", nargs="?", type=int, help="one call's id: its request and response")
    trace.add_argument("--kind", help="list only calls of this kind: match, employer, recruiters, cover_note")
    trace.add_argument("--last", type=_positive_int, help="how many calls to list (20)")

    identify = sub.add_parser(
        "identify",
        help="ask a model who is behind one posting -- pays (~$0.0002) unless already asked",
    )
    identify.add_argument("--vacancy", type=int, required=True, metavar="ID")
    identify.add_argument("--config", default=str(DEFAULT_PATH))

    recruiters = sub.add_parser(
        "recruiters",
        help="search the web for recruiters at the company behind one posting "
        "-- pays (~$0.01) unless a fresh search exists",
    )
    recruiters.add_argument("--vacancy", type=int, required=True, metavar="ID")
    recruiters.add_argument(
        "--refresh", action="store_true",
        help="search again even if a fresh search exists -- always pays",
    )
    recruiters.add_argument("--config", default=str(DEFAULT_PATH))

    args = parser.parse_args(argv)
    if args.command == "run":
        command = cmd_run(args.config, args.limit, match=not args.no_match, only=args.only)
    elif args.command == "discover":
        command = cmd_discover(args.config, args.only, args.limit)
    elif args.command == "fetch" and args.pending:
        command = cmd_fetch_pending(args.config, args.limit)
    elif args.command == "fetch":
        command = cmd_fetch(args.url)
    elif args.command == "cv-hash":
        command = cmd_cv_hash(args.config, args.cv)
    elif args.command == "match":
        command = cmd_match(args.config, dry_run=args.dry_run, cv=args.cv,
                            country=args.country, limit=args.limit)
    elif args.command == "trace":
        if args.id is not None and (args.kind or args.last):
            parser.error("trace: --kind and --last list calls; they do not apply to one id")
        command = cmd_trace(args.id, args.kind, args.last or 20)
    elif args.command == "identify":
        command = cmd_identify(args.config, args.vacancy)
    elif args.command == "recruiters":
        command = cmd_recruiters(args.config, args.vacancy, refresh=args.refresh)
    else:
        parser.error(f"unknown command {args.command}")  # unreachable
    return asyncio.run(_run(command))


async def _run(command: Coroutine[Any, Any, int]) -> int:
    """Close the pool on the loop that opened it; left to GC, it closes after the loop is gone."""
    try:
        return await command
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
