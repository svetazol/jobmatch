"""The docs must not name things that no longer exist.

`how-it-works.md` drifted into documenting four removed features -- a deleted
config file, a setting that had become a constant, a renamed module and a result
ceiling that turned out to be wrong -- and nothing noticed until someone read it.
This is the cheapest guard that makes doc accuracy self-enforcing.

Add to `GONE` whenever you remove something a doc might still mention. Keep the
reason: an entry nobody can explain is an entry nobody dares delete.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# symbol -> what replaced it
GONE = {
    "config.bg.toml": "named sweeps in config.toml",
    "max_fetch_attempts": "models.MAX_FETCH_ATTEMPTS",
    "sources/hh.py": "sources/hh/ (a package)",
    "800-result": "the ceiling is 2 000 per query",
    "800 results per query": "the ceiling is 2 000 per query",
    "source.params": "source.search",
    "sources[].params": "sources.<name>.search",
    "load_settings(path).sources[0].params": "…search",
    "regions_of(": "areas.coverage()",
    "how-it-works.md": "docs/pipeline.md",
    # the LangChain people search, dropped 2026-10-12 (docs/decisions.md)
    "people.py": "the employer identity call (docs/company-research.md)",
    "PeopleSearch": "nothing yet; see docs/decisions.md, People search",
    "find_people": "nothing yet; see docs/decisions.md, People search",
    # the employer identity call's module, renamed 2026-10-13 to make room for
    # the recruiters search (docs/company-research.md §3)
    "identify.py": "jobmatch/research/",
    "identify.ask": "research.ask_employer",
    "identify.fingerprint": "research.fingerprint(research.employer_request(...))",
    # one copy of the key and the hashing rule, 2026-10-14 (the llm/ package)
    "research.API_KEY_ENV": "llm.API_KEY_ENV, read through llm.api_key",
    "jev.API_KEY_ENV": "llm.API_KEY_ENV, read through llm.api_key",
    "identify.request_body": "research.employer_request",
    "identify.reachable": "research.reachable",
    "identify.Identity": "research.Identity",
    "identify.Poster": "research.Poster",
    # renamed 2026-10-14, when a website alone became enough to search by
    "NoCompanyName": "company_research.NothingToSearchFor",
    # split 2026-10-14; not bare "research.py", which company_research.py contains
    "jobmatch/research.py": "jobmatch/research/ and jobmatch/llm/openrouter.py",
    "`research.py`": "`research/` and `llm/openrouter.py`",
    "**research.py**": "research/ and llm/openrouter.py",
    "research._call": "llm.openrouter.ask",
    # split 2026-10-14; not bare "jev.py", which llm/jev.py contains
    "matching/jev.py": "llm/jev.py and matching/match.py",
    "`jev.py`": "`llm/jev.py` and `matching/match.py`",
    "fingerprint, cv_hash, content_hash, job": "match_vacancy no longer takes a fingerprint",
    # moved 2026-10-14: pipeline.py keeps only the run phases
    "pipeline.lookup_employer": "company_research.lookup_employer",
    "pipeline.identify_employer": "company_research.identify_employer",
    "pipeline.find_recruiters": "company_research.find_recruiters",
    "pipeline.stored_recruiters": "company_research.stored_recruiters",
    "pipeline.EmployerUnknown": "company_research.EmployerUnknown",
    "pipeline.NothingToSearchFor": "company_research.NothingToSearchFor",
    "pipeline.NotLookedUp": "company_research.NotLookedUp",
    "pipeline.poster_of": "company_research.poster_of",
    "pipeline.target_of": "company_research.target_of",
    "pipeline.RECRUITERS_FRESH": "company_research.RECRUITERS_FRESH",
    "pipeline._recruiters_request": "company_research._recruiters_request",
}

# Prose that happens to contain a dead string for a legitimate reason: the
# decisions log records what was removed, and the original brief is frozen.
EXEMPT = {"docs/decisions.md", "docs/initial_task.md"}


def tracked_docs() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [ROOT / line for line in out.stdout.split() if line not in EXEMPT]


def test_there_are_docs_to_check():
    """A bug in the git plumbing above would make every test below vacuous."""
    assert tracked_docs()


@pytest.mark.parametrize("dead, replacement", sorted(GONE.items()))
def test_no_doc_names_something_that_is_gone(dead, replacement):
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in tracked_docs()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if dead in line
    ]
    assert not offenders, f"{dead!r} is gone (now: {replacement}) -- see {offenders}"


def test_every_path_a_doc_points_at_exists():
    """A doc naming a file that moved is the same failure, one level up."""
    pattern = re.compile(r"`(jobmatch/[\w/.]+\.py|docs/[\w.-]+\.md|config\.toml)`")
    missing = []
    for path in tracked_docs():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for ref in pattern.findall(line):
                if not (ROOT / ref).exists():
                    missing.append(f"{path.relative_to(ROOT)}:{n} -> {ref}")
    assert not missing, f"docs point at paths that do not exist: {missing}"
