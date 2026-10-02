"""Engine and session factory — the only module that reads DATABASE_URL.

Async throughout. The same ``postgresql+psycopg://`` URL serves both modes,
which is why Alembic (``migrations/env.py``) can stay synchronous against it.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")

engine = create_async_engine(DATABASE_URL)
# expire_on_commit=False is load-bearing: rows are read after their session
# closes (the pipeline logs `vacancy.url` after the paid call), and under
# AsyncSession an expired attribute cannot lazy-load — it raises.
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)
