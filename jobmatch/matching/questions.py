"""What we ask Jev about a (cv, job_description) pair.

Each question is its own named constant so it can be read, added, removed, or
reused on its own; QUESTIONS is just the aggregate the API call sends.
"""
import hashlib
import json

from typesafe_sdk import Choice, Noul, NoulCriteria, Score

IS_QUALIFIED = Noul(
    instructions="Does the candidate meet the job's minimum required qualifications?",
    criteria=NoulCriteria(
        true="Candidate meets all must-have requirements.",
        false="Candidate is missing one or more must-have requirements.",
    ),
)

OVERALL_FIT = Score(
    instructions="How strong is the overall fit between the candidate's CV and the job description?",
    criteria=[
        {"label": "poor", "description": "Major mismatch in skills/experience."},
        {"label": "weak", "description": "Some relevant experience but significant gaps."},
        {"label": "good", "description": "Solid match with minor gaps."},
        {"label": "strong", "description": "Very close match to requirements."},
        {"label": "excellent", "description": "Ideal match, exceeds requirements."},
    ],
)

TOP_GAP = Choice(
    instructions="What is the single biggest gap, if any, between the candidate and this job?",
    criteria={
        "technical_skills": "Missing or weak technical/tooling skills.",
        "domain_experience": "Missing relevant industry or domain experience.",
        "seniority_level": "Under- or over-qualified for the seniority level.",
        "none": "No significant gap.",
    },
)

# The four angles from the master CV's positioning bank. One angle is picked
# per posting, never blended -- so this asks which one a vacancy calls for.
# "none" exists because plenty of postings fit no angle at all, and forcing a
# choice would make the answer look meaningful when it isn't.
BEST_ANGLE = Choice(
    instructions=(
        "Which single professional angle should the candidate lead with when applying "
        "to this job, given what the posting emphasises?"
    ),
    criteria={
        "backend": (
            "Generalist backend engineering: Python/Django/PostgreSQL depth, REST API "
            "design, background processing, production database migrations, performance work."
        ),
        "ai_llm": (
            "AI/LLM application engineering: production LLM API features, structured "
            "extraction, summaries, MCP tools and agentic workflows."
        ),
        "product": (
            "Product-minded end-to-end ownership: taking a feature from discovery through "
            "rollout and adoption, working with PMs on scope; judgment and delivery rather "
            "than depth in one stack."
        ),
        "data": (
            "Data-intensive work: data warehousing, ETL, analytics, MPP or columnar "
            "databases, large-scale data processing and performance tuning."
        ),
        "none": "The posting fits none of these angles.",
    },
)

QUESTIONS = {
    "is_qualified": IS_QUALIFIED,
    "overall_fit": OVERALL_FIT,
    "top_gap": TOP_GAP,
    "best_angle": BEST_ANGLE,
}


def canonical(questions: dict) -> str:
    """The exact serialisation that goes into a fingerprint. Sorted, compact."""
    payload = {k: q.model_dump(mode="json") for k, q in sorted(questions.items())}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# Changes whenever a question is added, removed or reworded — so there is no
# hand-maintained PROMPT_VERSION to forget to bump.
QUESTIONS_HASH = hashlib.sha256(canonical(QUESTIONS).encode()).hexdigest()
