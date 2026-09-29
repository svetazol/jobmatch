"""Request-scoped dependencies. One session per request, closed either way."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from ..db import SessionLocal


def get_session() -> Iterator[Session]:
    """A session per request.

    Read routes never commit; the triage route commits explicitly. Rolling back
    on the way out costs nothing on a read-only session and means a half-applied
    write can never escape a failed request.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
