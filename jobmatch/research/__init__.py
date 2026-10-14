"""Questions to a model about a company: who is behind a posting, and who recruits there.

The caches are `pipeline`'s; these always pay. Design: `docs/company-research.md`.
"""
from __future__ import annotations

# Re-exported so callers hash a request beside building it, without importing llm.
from ..llm import fingerprint
from .employer import (
    EMPLOYER,
    EMPLOYER_SYSTEM,
    Identity,
    Poster,
    ask_employer,
    employer_prompt,
    employer_request,
    reachable,
)
from .recruiters import (
    RECRUITERS,
    RECRUITERS_SYSTEM,
    Confirmed,
    ConfirmedPerson,
    Person,
    Recruiters,
    Target,
    ask_recruiters,
    cited,
    keep_cited,
    linkedin_slug,
    recruiters_prompt,
    recruiters_request,
)

__all__ = [
    "EMPLOYER",
    "EMPLOYER_SYSTEM",
    "RECRUITERS",
    "RECRUITERS_SYSTEM",
    "Confirmed",
    "ConfirmedPerson",
    "Identity",
    "Person",
    "Poster",
    "Recruiters",
    "Target",
    "ask_employer",
    "ask_recruiters",
    "cited",
    "employer_prompt",
    "employer_request",
    "fingerprint",
    "keep_cited",
    "linkedin_slug",
    "reachable",
    "recruiters_prompt",
    "recruiters_request",
]
