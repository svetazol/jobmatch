"""Request-scoped dependencies."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    """Closed without a commit, it rolls back, so a failed request leaves no half-applied write."""
    async with SessionLocal() as session:
        yield session
