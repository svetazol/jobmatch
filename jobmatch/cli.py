"""Command line entry point. A thin caller of pipeline + repository."""
from __future__ import annotations

import argparse
import logging
import sys

from . import pipeline, repository
from .config import DEFAULT_PATH, load_settings
from .db import SessionLocal
from .sources import source_for_url

log = logging.getLogger(__name__)


def cmd_run(config_path: str) -> int:
    settings = load_settings(config_path)
    report = pipeline.run(settings)
    with SessionLocal() as session:
        stored, fetched = repository.count_vacancies(session)
    print(f"\n{report.summary()}")
    print(f"database now holds {stored} vacancies, {fetched} of them fetched")
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="jobmatch")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="discover from the configured filter, then fetch")
    run.add_argument("--config", default=str(DEFAULT_PATH))

    fetch = sub.add_parser("fetch", help="scrape one vacancy page into the database")
    fetch.add_argument("url")

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args.config)
    if args.command == "fetch":
        return cmd_fetch(args.url)
    parser.error(f"unknown command {args.command}")  # unreachable


if __name__ == "__main__":
    sys.exit(main())
