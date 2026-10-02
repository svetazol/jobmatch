"""Request-scoped dependencies. One session per request, closed either way."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    """A session per request.

    Read routes never commit; the triage route commits explicitly. Closing
    without a commit rolls back, which costs nothing on a read-only session
    and means a half-applied write can never escape a failed request.
    """
    async with SessionLocal() as session:
        yield session
