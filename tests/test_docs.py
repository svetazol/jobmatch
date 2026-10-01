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
