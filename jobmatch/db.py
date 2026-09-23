"""Engine and session factory — the only module that reads DATABASE_URL."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")

engine = create_engine(DATABASE_URL, future=True)
SessionLocal: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
