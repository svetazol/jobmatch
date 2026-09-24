"""SQLAlchemy models — the single source of truth for the schema.

Alembic follows this file; nothing calls ``create_all`` outside tests.
Only the tables that are actually used exist here: ``MatchResult`` and
``LlmCall`` arrive with the matching task that needs them.
"""
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
    # Set once here rather than DateTime(timezone=True) on a dozen columns.
    type_annotation_map = {dt.datetime: DateTime(timezone=True)}


class Vacancy(Base):
    """One row per vacancy per source.

    Discovery and fetch are two phases, so a partially filled row (everything
    below ``url`` still NULL) is a first-class state, not a second table.
    """

    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # natural key & location
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
    # hh's own name for the country, as the posting states it; the ISO code
    # stays in `raw`. Not the searched area code: the "other regions" area is
    # a catch-all that returns real countries, so the page is the only honest
    # source.
    country: Mapped[str | None] = mapped_column(Text)
    # a set, not one value: a real posting can offer on-site, remote and
    # hybrid at once. Empty when the posting doesn't say.
    work_formats: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )

    # source-specific payload; the escape hatch that keeps the columns above
    # source-agnostic
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # sha256 of the exact job text sent to the matcher; filled by the matching
    # task, kept here because it describes the scrape, not the match
    content_hash: Mapped[str | None] = mapped_column(String(64))

    # lifecycle
    first_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    fetched_at: Mapped[dt.datetime | None] = mapped_column()
    fetch_attempts: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0")
    )
    fetch_error: Mapped[str | None] = mapped_column(Text)
    delisted_at: Mapped[dt.datetime | None] = mapped_column()

    # triage (single user: columns, not a table)
    seen_at: Mapped[dt.datetime | None] = mapped_column()
    starred_at: Mapped[dt.datetime | None] = mapped_column()
    hidden_at: Mapped[dt.datetime | None] = mapped_column()

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
                "fetched_at IS NULL AND delisted_at IS NULL AND fetch_attempts < 3"
            ),
        ),
    )

    matches: Mapped[list[MatchResult]] = relationship(
        back_populates="vacancy",
        cascade="all, delete-orphan",
        order_by="MatchResult.created_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Vacancy {self.source}:{self.external_id} {self.title!r}>"


class MatchResult(Base):
    """One row per *paid* Jev call that produced a usable answer.

    Append-only; the only update is setting ``superseded_at``. Operational
    facts about the call — cost, tokens, what went wrong — live on LlmCall.
    """

    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    llm_call_id: Mapped[int] = mapped_column(ForeignKey("llm_calls.id"))

    # idempotency
    inputs_fingerprint: Mapped[str] = mapped_column(String(64))
    # the three below are never looked up — they answer "why is this stale?"
    vacancy_content_hash: Mapped[str] = mapped_column(String(64))
    cv_hash: Mapped[str] = mapped_column(String(64))
    questions_hash: Mapped[str] = mapped_column(String(64))

    # promoted answers — a column only if the UI sorts, filters or lists by it
    is_qualified_noul: Mapped[float] = mapped_column(Double)
    is_qualified: Mapped[bool] = mapped_column(
        Boolean, Computed("is_qualified_noul >= 0.5", persisted=True)
    )  # read-only in Python; Alembic cannot autogenerate a change to this
    # Jev returns a position on the legend's index scale (0..levels-1, e.g.
    # 2.4 of 0..4). Stored normalised to 0–1 so the sort key and any min_fit
    # threshold survive a question set with a different number of levels; the
    # raw value stays in `answers`.
    overall_fit_score: Mapped[float] = mapped_column(Double)  # THE sort key
    overall_fit_label: Mapped[str] = mapped_column(String(32))  # display only, never ORDER BY
    overall_fit_confidence: Mapped[float] = mapped_column(Double)
    top_gap: Mapped[str] = mapped_column(String(64))
    top_gap_confidence: Mapped[float] = mapped_column(Double)
    # which of the master CV's positioning angles the posting calls for
    best_angle: Mapped[str] = mapped_column(String(32))
    best_angle_confidence: Mapped[float] = mapped_column(Double)

    answers: Mapped[dict[str, Any]] = mapped_column(JSONB)  # the complete record

    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    superseded_at: Mapped[dt.datetime | None] = mapped_column()

    vacancy: Mapped[Vacancy] = relationship(back_populates="matches")
    call: Mapped[LlmCall] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "vacancy_id", "inputs_fingerprint", name="uq_match_results_vacancy_fingerprint"
        ),
        # "the current match" is a DB-enforced fact, not a MAX(created_at) convention
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
        # the only thing that catches an SDK shape change writing garbage into
        # the sort key
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
    """One row per Jev call *attempted*, including the ones that failed.

    The operational ledger: what was asked, what it cost, how long it took and
    what went wrong. Nothing reads it to decide what to do next — this is for
    a human to read, not for the program to branch on.
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # SET NULL, not CASCADE: deleting a vacancy must not erase what it cost
    vacancy_id: Mapped[int | None] = mapped_column(
        ForeignKey("vacancies.id", ondelete="SET NULL"), index=True
    )

    inputs_fingerprint: Mapped[str] = mapped_column(String(64))
    model_requested: Mapped[str] = mapped_column(String(64))
    # not in the fingerprint — unknowable before paying for the call
    model_resolved: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(64))
    response_id: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(String(16))  # "ok" | "error"; plain text, see §11
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # Numeric, never Float: this column exists to be summed. 9 decimal places
    # because a real call costs $0.000413616 — (12,6) would round that to
    # $0.000414, and a cheaper call straight to zero.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 9))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

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
