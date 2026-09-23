"""SQLAlchemy models — the single source of truth for the schema.

Alembic follows this file; nothing calls ``create_all`` outside tests.
Only the tables that are actually used exist here: ``MatchResult`` and
``LlmCall`` arrive with the matching task that needs them.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Index,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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

    def __repr__(self) -> str:
        return f"<Vacancy {self.source}:{self.external_id} {self.title!r}>"
