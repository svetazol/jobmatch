"""The schema's source of truth; Alembic follows it, and only tests call ``create_all``."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Baked into ix_vacancies_fetch_queue's predicate: changing it needs a migration, not a config knob.
MAX_FETCH_ATTEMPTS = 3

# Deterministic constraint names, so Alembic autogenerate stays stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dt.datetime: DateTime(timezone=True)}


class Vacancy(Base):
    """One row per vacancy per source; NULLs below ``url`` mean discovered but not yet fetched."""

    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)

    # scraped core (NULL until the detail fetch succeeds)
    title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    salary_raw: Mapped[str | None] = mapped_column(Text)
    experience_raw: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    published_at: Mapped[dt.datetime | None] = mapped_column()
    # From the page, not the searched area: "other regions" returns real countries.
    country: Mapped[str | None] = mapped_column(Text)
    # A set: one posting can offer on-site, remote and hybrid at once.
    work_formats: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    # Joins employers on (source, external_id) with no FK; see docs/architecture.md §3.5.
    employer_external_id: Mapped[str | None] = mapped_column(String(128))

    # Source-specific payload, so the columns above stay source-agnostic.
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # sha256 of the job text sent to the matcher: it describes the scrape, not the match.
    content_hash: Mapped[str | None] = mapped_column(String(64))

    first_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    fetched_at: Mapped[dt.datetime | None] = mapped_column()
    fetch_attempts: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0")
    )
    fetch_error: Mapped[str | None] = mapped_column(Text)
    delisted_at: Mapped[dt.datetime | None] = mapped_column()

    # Triage: single user, so columns, not a table.
    seen_at: Mapped[dt.datetime | None] = mapped_column()
    starred_at: Mapped[dt.datetime | None] = mapped_column()
    hidden_at: Mapped[dt.datetime | None] = mapped_column()
    applied_at: Mapped[dt.datetime | None] = mapped_column()  # set by hand: hh is not asked

    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", name="uq_vacancies_source_external_id"
        ),
        Index("ix_vacancies_url", "url"),
        Index(
            "ix_vacancies_fetch_queue",
            "first_seen_at",
            postgresql_where=text(
                "fetched_at IS NULL AND delisted_at IS NULL AND "
                f"fetch_attempts < {MAX_FETCH_ATTEMPTS}"
            ),
        ),
    )

    matches: Mapped[list[MatchResult]] = relationship(
        back_populates="vacancy",
        cascade="all, delete-orphan",
        lazy="raise",   # async: an unloaded relationship must fail loudly, not await
        order_by="MatchResult.created_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Vacancy {self.source}:{self.external_id} {self.title!r}>"


class Employer(Base):
    """One row per employer someone looked up; the crawl never writes one."""

    __tablename__ = "employers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # Keyed by the site's id, never the name, as vacancies are.
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)

    name: Mapped[str | None] = mapped_column(Text)
    # What the employer links, http(s) only; often a careers site, not the homepage.
    site_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )

    looked_up_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", name="uq_employers_source_external_id"
        ),
    )

    def __repr__(self) -> str:
        return f"<Employer {self.source}:{self.external_id} {self.name!r}>"


class EmployerIdentity(Base):
    """Who is behind a posting, per posting as it is in the request; see docs/architecture.md §3.6."""

    __tablename__ = "employer_identities"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    # The cache key; unique because the posting is inside the request.
    inputs_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    # Whole, as nothing filters on its fields.
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Checked once on store, so the UI links only a URL that answered; NULL when there is none.
    homepage_ok: Mapped[bool | None] = mapped_column(Boolean)
    careers_ok: Mapped[bool | None] = mapped_column(Boolean)
    llm_call_id: Mapped[int] = mapped_column(ForeignKey("llm_calls.id"))

    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())

    def __repr__(self) -> str:
        return f"<EmployerIdentity v{self.vacancy_id} {self.answer.get('employer')!r}>"


class RecruiterSearch(Base):
    """Who recruits for a company, per employer and request; see docs/architecture.md §3.7."""

    __tablename__ = "recruiter_searches"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    employer_id: Mapped[int] = mapped_column(ForeignKey("employers.id", ondelete="CASCADE"))
    # Not unique: a stale search is asked again with the same inputs, as people change jobs.
    inputs_fingerprint: Mapped[str] = mapped_column(String(64))
    # Only the cited people, whole, as nothing filters on its fields.
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB)
    llm_call_id: Mapped[int] = mapped_column(ForeignKey("llm_calls.id"))

    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # A handful per employer at most, so the fingerprint need not be indexed.
        Index("ix_recruiter_searches_employer_created", "employer_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<RecruiterSearch e{self.employer_id} {len(self.answer.get('people', []))} people>"


class RecruiterContact(Base):
    """That the user wrote to someone; keyed by person, so it outlives every search; §3.8."""

    __tablename__ = "recruiter_contacts"

    # Canonical (`research.linkedin_profile_url`), or a mark would miss the person it marks.
    linkedin_url: Mapped[str] = mapped_column(Text, primary_key=True)
    written_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())

    def __repr__(self) -> str:
        return f"<RecruiterContact {self.linkedin_url} {self.written_at:%Y-%m-%d}>"


class MatchResult(Base):
    """One row per paid Jev call with a usable answer; append-only apart from ``superseded_at``."""

    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    llm_call_id: Mapped[int] = mapped_column(ForeignKey("llm_calls.id"))

    inputs_fingerprint: Mapped[str] = mapped_column(String(64))
    # Never looked up: they answer "why is this stale?"
    vacancy_content_hash: Mapped[str] = mapped_column(String(64))
    cv_hash: Mapped[str] = mapped_column(String(64))
    questions_hash: Mapped[str] = mapped_column(String(64))

    # A column only if the UI sorts, filters or lists by it.
    is_qualified_noul: Mapped[float] = mapped_column(Double)
    is_qualified: Mapped[bool] = mapped_column(
        Boolean, Computed("is_qualified_noul >= 0.5", persisted=True)
    )  # read-only in Python; Alembic cannot autogenerate a change to this
    # Normalised to 0-1 so it survives a different number of levels; the raw value is in `answers`.
    overall_fit_score: Mapped[float] = mapped_column(Double)  # THE sort key
    overall_fit_label: Mapped[str] = mapped_column(String(32))  # display only, never ORDER BY
    overall_fit_confidence: Mapped[float] = mapped_column(Double)
    top_gap: Mapped[str] = mapped_column(String(64))
    top_gap_confidence: Mapped[float] = mapped_column(Double)
    best_angle: Mapped[str] = mapped_column(String(32))
    best_angle_confidence: Mapped[float] = mapped_column(Double)

    answers: Mapped[dict[str, Any]] = mapped_column(JSONB)  # the complete record

    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    superseded_at: Mapped[dt.datetime | None] = mapped_column()

    vacancy: Mapped[Vacancy] = relationship(back_populates="matches", lazy="raise")
    call: Mapped[LlmCall] = relationship(lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "vacancy_id", "inputs_fingerprint", name="uq_match_results_vacancy_fingerprint"
        ),
        # "The current match" is enforced by the DB, not a MAX(created_at) convention.
        Index(
            "uq_match_results_current",
            "vacancy_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index(
            "ix_match_results_current_rank",
            text("overall_fit_score DESC"),
            "vacancy_id",
            postgresql_where=text("superseded_at IS NULL"),
        ),
        # The only guard against an SDK shape change writing garbage into the sort key.
        CheckConstraint(
            "overall_fit_score BETWEEN 0 AND 1 AND is_qualified_noul BETWEEN 0 AND 1 "
            "AND overall_fit_confidence BETWEEN 0 AND 1 AND top_gap_confidence BETWEEN 0 AND 1 "
            "AND best_angle_confidence BETWEEN 0 AND 1",
            name="probabilities_in_range",
        ),
    )

    def __repr__(self) -> str:
        return f"<MatchResult v{self.vacancy_id} {self.overall_fit_label} {self.overall_fit_score:.2f}>"


class LlmCall(Base):
    """One row per model call attempted, failed or not: a ledger for a human, never branched on."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # SET NULL, not CASCADE: deleting a vacancy must not erase what it cost.
    vacancy_id: Mapped[int | None] = mapped_column(
        ForeignKey("vacancies.id", ondelete="SET NULL"), index=True
    )

    # Which caller paid; plain text so a new caller needs no migration, no default so it must say.
    kind: Mapped[str] = mapped_column(String(16))
    inputs_fingerprint: Mapped[str] = mapped_column(String(64))
    model_requested: Mapped[str] = mapped_column(String(64))
    # Not in the fingerprint: unknowable before paying.
    model_resolved: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(64))
    response_id: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(String(16))  # "ok" | "error"
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # Numeric to be summed; 9 places because a real call costs $0.000413616.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 9))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # The body is kept even when it failed to parse: it is the evidence of what was paid for.
    request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    started_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_llm_calls_started_at", text("started_at DESC")),
        Index(
            "ix_llm_calls_errors",
            text("started_at DESC"),
            postgresql_where=text("status = 'error'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<LlmCall {self.status} {self.model_resolved} ${self.cost_usd}>"
