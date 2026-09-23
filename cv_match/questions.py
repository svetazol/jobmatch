"""What we ask Jev about a (cv, job_description) pair.

Each question is its own named constant so it can be read, added, removed, or
reused on its own; QUESTIONS is just the aggregate the API call sends.
"""
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

QUESTIONS = {
    "is_qualified": IS_QUALIFIED,
    "overall_fit": OVERALL_FIT,
    "top_gap": TOP_GAP,
}
