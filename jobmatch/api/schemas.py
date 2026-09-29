"""Response models: projections shaped for one screen, not a second schema.

§13 rejects "pydantic domain models mirroring the ORM models", and these do not
mirror them — they deliberately omit `raw`, every hash, `fetch_error` and the
rest of what the UI never shows. Field names match the columns so there is
nothing to translate on the client side.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class MatchSummary(BaseModel):
    """The promoted answers, each with the confidence that qualifies it.

    Confidence is not optional decoration. In the live corpus the top-scoring
    vacancy holds `top_gap = 'none'` at confidence .17 with the four choices
    near-tied; an API that returned the winner alone would make it impossible
    for the client to refuse to print it as a fact.
    """

    model_config = ConfigDict(from_attributes=True)

    overall_fit_score: float
    overall_fit_label: str
    overall_fit_confidence: float
    is_qualified: bool
    is_qualified_noul: float
    top_gap: str
    top_gap_confidence: float
    best_angle: str
    best_angle_confidence: float
    fit_probabilities: dict[str, float] = {}


class VacancyRow(BaseModel):
    """One row of the ranked list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    company: str | None
    country: str | None
    work_formats: list[str]
    url: str
    source: str
    published_at: dt.datetime | None
    seen_at: dt.datetime | None
    starred_at: dt.datetime | None
    hidden_at: dt.datetime | None
    delisted_at: dt.datetime | None
    stale: bool
    # None when the vacancy is fetched but not matched yet — a first-class
    # state (§3.1), not a score of zero.
    match: MatchSummary | None


class VacancyDetail(VacancyRow):
    """The row plus what only the detail view needs."""

    salary_raw: str | None
    experience_raw: str | None
    description: str | None
    skills: list[str]
    fetched_at: dt.datetime | None
    answers: dict[str, Any] | None
    cost_usd: Decimal | None


class VacancyPage(BaseModel):
    items: list[VacancyRow]
    total: int
    next_cursor: str | None


class PitchStat(BaseModel):
    pitch: str
    n: int
    mean_fit: float
    qualified: int
    # counts per fit level in ladder order, poor -> excellent
    distribution: list[int]


class NameCount(BaseModel):
    name: str
    n: int


class CvOption(BaseModel):
    """One CV the corpus has answers from.

    `name` is the file it came from when that file is still on disk, and the
    truncated hash when it is not — a CV that has been renamed or deleted
    still owns its answers, and hiding it would hide the matches with it.
    `name` is also what `?cv=` takes, so the picker round-trips.
    """

    name: str
    cv_hash: str
    n: int                       # current matches stored under it
    last_matched: dt.datetime | None
    on_disk: bool


class Search(BaseModel):
    """What produced this corpus.

    The whole point of showing it: at a 9.6% signal rate the search terms are
    the highest-leverage thing on the screen, and a reader who cannot see them
    has no way to know why two thirds of the postings are irrelevant.
    """

    source: str
    keyword: str            # hh's `text` param
    fields: list[str]       # where it looked — title, description
    excluded: list[str]     # terms that disqualify a posting outright


class Stats(BaseModel):
    # Which CV these numbers answer for, and which others could be asked.
    # Never absent: every figure below is about one CV, and a page that does
    # not say which is a page that cannot be checked.
    cv: CvOption | None
    cvs: list[CvOption]
    total: int
    qualified: int
    worth_applying: int
    median_fit: float
    mean_fit: float
    spend_usd: float
    calls: int
    avg_duration_ms: int
    fit_distribution: dict[str, int]
    pitches: list[PitchStat]
    countries: list[NameCount]
    work_formats: list[NameCount]
    search: Search


class TriagePatch(BaseModel):
    """Exactly one field per request; `false` clears the timestamp (Undo)."""

    seen: bool | None = None
    starred: bool | None = None
    hidden: bool | None = None
