"""A short cover letter for one vacancy, written by `claude -p` on request.

A subprocess, unlike the matcher (§8): this runs on the user's own logged-in
Claude Code (`jobmatch/llm/claude_cli.py`), so it needs no key and spends no
OpenRouter credit -- and it only works on a machine where `claude` is installed
and signed in. A hosted version would call the Anthropic API with the same
prompt instead.

The note itself is not stored: it is a draft to copy and edit, and regenerating
one is a click. The call is, like every model call, in `llm_calls`.
"""
from __future__ import annotations

import re

from .llm import claude_cli
from .matching.questions import BEST_ANGLE, TOP_GAP
from .models import MatchResult, Vacancy

MODEL = "claude-sonnet-5-5"   # pinned, not the "sonnet" alias: it is in the fingerprint

# Below this the matcher's gap is a near-tie (see VacancyDetailView's "vague"
# rule); naming it would make the note answer an objection nobody raised.
_GAP_CONFIDENT = 0.5

_SYSTEM = """\
You write short cover letters for a job seeker applying to one specific
vacancy. Rules:
- Use only facts present in the CV. Never invent employers, numbers, tools or
  years. If the CV lacks something the vacancy asks for, do not claim it.
- Shape: a short greeting line; an opening sentence naming the role (and the
  company, if given) and why it caught their interest; a body of 2-3 sentences
  on the 2-3 strongest matches with the vacancy's requirements; one closing
  sentence inviting a conversation; a sign-off with the candidate's name from
  the CV (just the sign-off word if the CV has no name).
- Short: at most 120 words in all. A recruiter should read it in under a
  minute. At most one number in the whole letter.
- Plain, simple words; no jargon piles, no long lists of technologies, no
  clichés like "I am writing to apply" or "I believe I would be a great fit".
- First person, warm and confident. No bullet points, no headings, no subject
  line, no placeholders such as [Company] or [Name].
- Write in the language you are told to use.
- Output the letter text only."""

# The candidate is a woman. English grammar does not show it; Russian past
# tense and short adjectives do ("проектировала", "готова").
_GENDER = {
    "Russian": "The candidate is a woman: use feminine forms throughout "
               "(e.g. «я проектировала», «я готова»).",
}


def language_of(vacancy: Vacancy) -> str:
    """Russian if the posting is mostly Cyrillic, else English.

    Decided here rather than left to the model: hh postings mix English tech
    terms into Russian prose, and "the vacancy's language" is then a judgment
    call this makes the same way every time.
    """
    text = " ".join(filter(None, [vacancy.title, vacancy.description]))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "Russian" if cyrillic > latin else "English"


def build_prompt(vacancy: Vacancy, match: MatchResult | None, cv: str) -> str:
    language = language_of(vacancy)
    parts = [" ".join(filter(None, [f"Write the note in {language}.", _GENDER.get(language)]))]
    if match is not None and match.best_angle != "none":
        parts.append(
            "Lead with this angle, which a matcher judged the strongest for this "
            f"posting: {BEST_ANGLE.criteria[match.best_angle]}"
        )
    if match is not None and match.top_gap != "none" and match.top_gap_confidence >= _GAP_CONFIDENT:
        parts.append(
            f"The likely weak spot is: {TOP_GAP.criteria[match.top_gap]} Do not mention "
            "it; where the CV honestly offsets it, let that show."
        )
    vacancy_text = "\n".join(filter(None, [
        f"Title: {vacancy.title}",
        f"Company: {vacancy.company}" if vacancy.company else None,
        f"Key skills: {', '.join(vacancy.skills)}" if vacancy.skills else None,
        f"Experience: {vacancy.experience_raw}" if vacancy.experience_raw else None,
        "",
        vacancy.description or "",
    ]))
    parts.append(f"<vacancy>\n{vacancy_text}\n</vacancy>")
    parts.append(f"<cv>\n{cv}\n</cv>")
    return "\n\n".join(parts)


async def write(vacancy: Vacancy, match: MatchResult | None, cv: str) -> str:
    """The note, from one recorded `claude -p` call; a `claude_cli.ClaudeError` when it fails."""
    return await claude_cli.ask(
        MODEL, _SYSTEM, build_prompt(vacancy, match, cv), kind="cover_note", vacancy_id=vacancy.id
    )
