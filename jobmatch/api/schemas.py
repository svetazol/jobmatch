"""Response models: per-screen projections, not mirrors of the ORM (docs/architecture.md §11)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..sources.hh import Seniority


class MatchSummary(BaseModel):
    """Each answer with its confidence, so the client can refuse to print a near-tie as a fact."""

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
    applied_at: dt.datetime | None
    delisted_at: dt.datetime | None
    stale: bool
    # None when fetched but not matched yet, not a score of zero.
    match: MatchSummary | None


class VacancyDetail(VacancyRow):
    """The row plus what only the detail view needs."""

    salary_raw: str | None
    experience_raw: str | None
    description: str | None
    skills: list[str]
    fetched_at: dt.datetime | None
    # What `stale` is measured from, so the UI can say since when.
    last_seen_at: dt.datetime
    answers: dict[str, Any] | None
    # hh's experience bucket against `experience_years`; None when either is unknown.
    seniority: Seniority | None


class VacancyPage(BaseModel):
    items: list[VacancyRow]
    total: int
    next_cursor: str | None


class PitchStat(BaseModel):
    pitch: str
    n: int
    mean_fit: float
    qualified: int
    # Counts per fit level, poor -> excellent.
    distribution: list[int]


class NameCount(BaseModel):
    name: str
    n: int


class CvOption(BaseModel):
    """`name` is the file, or the short hash once it is gone; it is also what `?cv=` takes."""

    name: str
    cv_hash: str
    n: int                       # current matches stored under it
    last_matched: dt.datetime | None
    on_disk: bool


class Search(BaseModel):
    """Shown because at a 9.6% signal rate the search terms explain the irrelevant postings."""

    source: str
    keyword: str            # hh's `text` param
    fields: list[str]
    excluded: list[str]


class Stats(BaseModel):
    # Every figure is about one CV, so the page must say which.
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
    applied: bool | None = None


class CoverNote(BaseModel):
    text: str
    language: str


class CompanyIdentity(BaseModel):
    """The stored `research.Identity` (None = unknown), whether each link answered, and its call."""

    employer: str | None
    homepage: str | None
    careers_page: str | None
    is_agency: bool | None
    client: str | None
    notes: str
    homepage_ok: bool | None
    careers_ok: bool | None
    model: str
    cost_usd: float | None
    llm_call_id: int
    created_at: dt.datetime


class RecruiterPerson(BaseModel):
    """`linkedin_url` is rebuilt from the slug, so the model's own text never reaches an href."""

    name: str
    role: str | None
    linkedin_url: str
    evidence: str
    source_title: str | None
    written_at: dt.datetime | None   # set by hand, as `applied_at` is


class RecruiterWritten(BaseModel):
    """Any form of a profile URL; `false` deletes the mark (Undo)."""

    linkedin_url: str
    written: bool


class CompanyRecruiters(BaseModel):
    """The search a posting's request would reuse; model and cost are in the trace, not the card."""

    people: list[RecruiterPerson]
    dropped: int
    sources: int
    created_at: dt.datetime


class Company(BaseModel):
    """The employer as its page said when last looked up; `city` and the rest are hh's own text."""

    name: str | None
    url: str
    site_url: str | None
    city: str | None
    category: str | None
    industries: str | None
    looked_up_at: dt.datetime
    identity: CompanyIdentity | None = None
    recruiters: CompanyRecruiters | None = None
