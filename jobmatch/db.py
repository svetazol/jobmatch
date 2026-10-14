"""The engine and session factory; the only reader of DATABASE_URL outside Alembic."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")

engine = create_async_engine(DATABASE_URL)
# Load-bearing: rows are read after their session closes, and an expired async attribute raises.
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)
