"""A vacancy as the models read it."""
from __future__ import annotations

from ..models import Vacancy


def job_text(vacancy: Vacancy) -> str:
    """Render a vacancy for the model. Goes to Jev, never to disk.

    Salary and skills are genuinely absent on a fair share of postings, so
    every line is conditional — an empty "Skills:" line is noise the model
    would have to interpret.
    """
    lines = [f"# {vacancy.title}"]
    for label, value in (
        ("Company", vacancy.company),
        ("Salary", vacancy.salary_raw),
        ("Experience", vacancy.experience_raw),
    ):
        if value:
            lines.append(f"{label}: {value}")
    lines += ["", vacancy.description or ""]
    if vacancy.skills:
        lines += ["", "## Key skills", ""] + [f"- {s}" for s in vacancy.skills]
    return "\n".join(lines).strip()

