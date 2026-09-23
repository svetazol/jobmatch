"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import sys

from .db import SessionLocal
from .repository import upsert_vacancy
from .sources import source_for_url

log = logging.getLogger(__name__)


def cmd_fetch(url: str) -> int:
    source = source_for_url(url)
    data = source.fetch(url)
    with SessionLocal() as session, session.begin():
        vacancy = upsert_vacancy(session, source.name, data)
        print(
            f"#{vacancy.id}  {vacancy.source}:{vacancy.external_id}\n"
            f"  title       {vacancy.title}\n"
            f"  company     {vacancy.company}\n"
            f"  salary      {vacancy.salary_raw}\n"
            f"  experience  {vacancy.experience_raw}\n"
            f"  published   {vacancy.published_at}\n"
            f"  skills      {', '.join(vacancy.skills) or '-'}\n"
            f"  description {len(vacancy.description or '')} chars\n"
            f"  raw keys    {', '.join(vacancy.raw) or '-'}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="jobmatch")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="scrape one vacancy page into the database")
    fetch.add_argument("url")

    args = parser.parse_args(argv)
    if args.command == "fetch":
        return cmd_fetch(args.url)
    parser.error(f"unknown command {args.command}")  # unreachable


if __name__ == "__main__":
    sys.exit(main())
