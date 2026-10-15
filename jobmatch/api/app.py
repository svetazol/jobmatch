"""FastAPI over the pipeline's database, inside the package so the schema has one codebase."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..db import engine
from .routers import company, stats, triage, vacancies


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Close the pool on the loop that opened it, not at interpreter exit."""
    yield
    await engine.dispose()


app = FastAPI(
    lifespan=lifespan,
    title="jobmatch",
    version="0.1.0",
    summary="Ranked vacancies and what the matcher concluded about them.",
)

app.include_router(vacancies.router)
app.include_router(triage.router)
app.include_router(stats.router)
app.include_router(company.router)
app.include_router(company.recruiters_router)


@app.get("/api/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


# No CORS and no auth on purpose: single user, and Vite proxies /api in dev.
