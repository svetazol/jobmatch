"""Command line entry point. A thin caller of pipeline + repository."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from . import matching, pipeline, repository
from .config import DEFAULT_PATH, Settings, load_settings
from .db import SessionLocal
from .models import MatchResult, Vacancy
from .sources import source_for_url

log = logging.getLogger(__name__)


def cmd_run(config_path: str, limit: int | None, match: bool = True) -> int:
    settings = load_settings(config_path)
    report = pipeline.run(settings, limit=limit, match=match)
    with SessionLocal() as session:
        stored, fetched, matched = repository.count_vacancies(session)
    print(f"\n{report.summary()}")
    print(
        f"database now holds {stored} vacancies, {fetched} fetched, "
        f"{matched} with a current match"
    )
    for url, exc in report.failures:
        print(f"  ! {url}: {type(exc).__name__}: {exc}")
    return 0


def cmd_fetch(url: str) -> int:
    source = source_for_url(url)
    data = source.fetch(url)
    with SessionLocal.begin() as session:
        vacancy = repository.store_fetched(session, source.name, data)
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


def cmd_match(config_path: str, *, dry_run: bool, cv: str | None,
              country: str | None, limit: int | None) -> int:
    settings = load_settings(config_path)
    if cv:
        settings = dataclasses.replace(settings, cv_path=Path(cv))
    if dry_run:
        return _dry_run(settings, country, limit)

    report = pipeline.match_all(settings, limit=limit, country=country)
    print(f"\nmatched {report.matched} (${report.cost:.6f}), "
          f"already matched {report.already_matched}, failed {len(report.failures)}")
    for url, exc in report.failures:
        print(f"  ! {url}: {type(exc).__name__}: {exc}")
    return 0


def _dry_run(settings: Settings, country: str | None, limit: int | None) -> int:
    """Ask Jev and print, writing nothing at all.

    No `llm_calls` row, no `match_results` row — which is the point, and also
    the catch: the spend is invisible to the ledger afterwards, so the total is
    printed here and nowhere else.
    """
    with SessionLocal() as session:
        vacancy_ids = repository.matchable_vacancy_ids(
            session, [entry.name for entry in settings.sources], country
        )[: limit or 10]
        vacancies = list(session.scalars(select(Vacancy).where(Vacancy.id.in_(vacancy_ids))))
        stored = {
            m.vacancy_id: m
            for m in session.scalars(
                select(MatchResult).where(
                    MatchResult.vacancy_id.in_(vacancy_ids),
                    MatchResult.superseded_at.is_(None),
                )
            )
        }

    cv = matching.load_cv(settings.cv_path)
    print(f"dry run — {settings.cv_path} ({len(cv)} chars), model {settings.model}, "
          f"{len(vacancies)} vacancies. Nothing will be written.\n")

    total = Decimal(0)
    with matching.open_client() as client:
        for vacancy in vacancies:
            preview = matching.preview_vacancy(
                client, cv, vacancy, settings.model, job=matching.job_text(vacancy)
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


def cmd_cv_hash(config_path: str, cv: str | None) -> int:
    """Print the hash a CV's answers are stored under — what `stats_cv_hash`
    wants, and the only way to tell which of several CVs a stored match
    belongs to. Hashed after sanitising, exactly as matching does it."""
    settings = load_settings(config_path)
    path = Path(cv) if cv else settings.cv_path
    digest = matching.sha256(matching.load_cv(path))
    with SessionLocal() as session:
        current = session.scalar(
            select(func.count())
            .select_from(MatchResult)
            .where(MatchResult.cv_hash == digest, MatchResult.superseded_at.is_(None))
        )
    print(f"{digest}  {path}  ({current} current matches)")
    return 0


def cmd_fetch_pending(config_path: str, limit: int | None) -> int:
    """Drain the fetch queue. No crawling, no spending."""
    settings = load_settings(config_path)
    report = pipeline.fetch_all(settings, limit=limit)
    with SessionLocal() as session:
        stored, fetched, matched = repository.count_vacancies(session)
        remaining = len(repository.fetch_queue(session, [e.name for e in settings.sources]))
    print(f"\n{report.summary()}")
    print(f"database now holds {stored} vacancies, {fetched} fetched; {remaining} still queued")
    for url, exc in report.failures[:10]:
        print(f"  ! {url}: {type(exc).__name__}: {exc}")
    if len(report.failures) > 10:
        print(f"  ... and {len(report.failures) - 10} more")
    return 0


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

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args.config, args.limit, match=not args.no_match)
    if args.command == "fetch" and args.pending:
        return cmd_fetch_pending(args.config, args.limit)
    if args.command == "fetch":
        return cmd_fetch(args.url)
    if args.command == "cv-hash":
        return cmd_cv_hash(args.config, args.cv)
    if args.command == "match":
        return cmd_match(args.config, dry_run=args.dry_run, cv=args.cv,
                         country=args.country, limit=args.limit)
    parser.error(f"unknown command {args.command}")  # unreachable


if __name__ == "__main__":
    sys.exit(main())
